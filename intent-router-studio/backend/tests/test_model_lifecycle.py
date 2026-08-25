"""ACTIVE 模型 / DB 指针 / 运行时缓存一致性（修改方案 V2 §3.5）。

不变量：
- 当前 ACTIVE 模型禁止归档（CANNOT_ARCHIVE_ACTIVE），指针不被悄悄置空；
- 运行时缓存命中必须校验 model_version_id 与 project.active_model_id 一致，
  指针已被更换时弃用陈旧缓存重载（含指针为 None 的越权注入）；
- 激活/回滚先加载冒烟再单事务切换：加载失败旧状态完全不变，
  回滚不再产生 ARCHIVED→CANDIDATE→ACTIVE 的中间态；
- 激活/回滚/停用按项目互斥，结构化审计事件落库。
"""
from __future__ import annotations

import threading

import pytest

from app import ids
from app.constants import ModelStatus
from app.db import SessionLocal
from app.errors import ApiError
from app.models import DatasetVersion, ModelVersion, Project, TrainingRun
from app.services import inference_service, run_service


class _FakeRuntime:
    """跳过真实模型加载的假运行时（只暴露指针一致性检查需要的字段）。"""

    def __init__(self, model_version_id: str) -> None:
        self.model_version_id = model_version_id
        self.threshold_version_id = None


def _fake_loader(model):
    return _FakeRuntime(model.id)


@pytest.fixture(autouse=True)
def _clean_runtime():
    yield
    inference_service.RUNTIME._runtimes.clear()
    inference_service.RUNTIME.cache.clear()


def _register_model(db, project_id: str) -> ModelVersion:
    """注册一个 CANDIDATE 模型行（FK 链 Project → DatasetVersion → TrainingRun → ModelVersion）。"""
    dataset = DatasetVersion(
        id=ids.prefixed(ids.DATASET),
        project_id=project_id,
        status="FROZEN",
        parquet_path="/nonexistent/lifecycle.parquet",
    )
    db.add(dataset)
    db.flush()
    run = TrainingRun(
        id=ids.prefixed(ids.RUN),
        project_id=project_id,
        dataset_id=dataset.id,
        config={},
        status="SUCCEEDED",
    )
    db.add(run)
    db.flush()
    model = ModelVersion(
        id=ids.prefixed(ids.MODEL),
        project_id=project_id,
        run_id=run.id,
        status=ModelStatus.CANDIDATE,
        artifact_path="/nonexistent/lifecycle-model",
        manifest_hash="0" * 64,
        manifest={},
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


# ---------------------------------------------------------------- 归档保护

def test_archive_active_model_blocked(db, project_id, monkeypatch):
    a = _register_model(db, project_id)
    monkeypatch.setattr(inference_service, "_load_model_runtime", _fake_loader)
    inference_service.activate_model(db, a.id)

    with pytest.raises(ApiError) as exc:
        run_service.archive_model(db, a.id)
    assert exc.value.code == "CANNOT_ARCHIVE_ACTIVE"
    assert exc.value.status_code == 409

    db.expire_all()
    assert db.get(ModelVersion, a.id).status == ModelStatus.ACTIVE
    assert db.get(Project, project_id).active_model_id == a.id
    # 运行时缓存不受影响
    assert inference_service.RUNTIME.get(project_id).model_version_id == a.id


def test_archive_non_active_model_allowed_and_audited(db, project_id, monkeypatch):
    a = _register_model(db, project_id)
    b = _register_model(db, project_id)
    monkeypatch.setattr(inference_service, "_load_model_runtime", _fake_loader)
    inference_service.activate_model(db, a.id)

    archived = run_service.archive_model(db, b.id)
    assert archived.status == ModelStatus.ARCHIVED
    # 重复归档幂等
    again = run_service.archive_model(db, b.id)
    assert again.status == ModelStatus.ARCHIVED

    events = [e for e in inference_service.list_audit_events(db, project_id) if e.event == "model_archived"]
    assert len(events) == 1
    assert events[0].from_model_id == b.id
    assert events[0].to_model_id is None
    assert events[0].details["previous_status"] == ModelStatus.CANDIDATE


# ---------------------------------------------------------------- 缓存与指针一致

def test_stale_runtime_evicted_on_pointer_change(db, project_id, monkeypatch):
    old = _register_model(db, project_id)
    new = _register_model(db, project_id)
    old.status = ModelStatus.ACTIVE
    project = db.get(Project, project_id)
    project.active_model_id = old.id
    db.commit()

    # 模拟指针被外部路径更换前的陈旧缓存：缓存是 new，指针是 old
    inference_service.RUNTIME.set(project_id, _FakeRuntime(new.id))

    loads: list[str] = []

    def loader(model):
        loads.append(model.id)
        return _FakeRuntime(model.id)

    monkeypatch.setattr(inference_service, "_load_model_runtime", loader)
    runtime = inference_service.ensure_project_runtime(db, project_id)
    assert runtime.model_version_id == old.id
    assert loads == [old.id]  # 陈旧缓存被弃用，按指针重载
    assert inference_service.RUNTIME.get(project_id).model_version_id == old.id


def test_runtime_without_pointer_rejected(db, project_id):
    # 指针为 None 时，任何注入的运行时都视为陈旧：绝不用无主模型回答请求
    inference_service.RUNTIME.set(project_id, _FakeRuntime("mdl_ghost000000000000"))
    with pytest.raises(ApiError) as exc:
        inference_service.ensure_project_runtime(db, project_id)
    assert exc.value.code == "MODEL_NOT_ACTIVE"
    assert inference_service.RUNTIME.get(project_id) is None


def test_pointer_to_non_active_model_rejected(db, project_id, monkeypatch):
    a = _register_model(db, project_id)
    project = db.get(Project, project_id)
    project.active_model_id = a.id
    db.commit()  # a 仍是 CANDIDATE（模拟直改 DB 造成的状态漂移）
    monkeypatch.setattr(inference_service, "_load_model_runtime", _fake_loader)
    with pytest.raises(ApiError) as exc:
        inference_service.ensure_project_runtime(db, project_id)
    assert exc.value.code == "MODEL_NOT_ACTIVE"


# ---------------------------------------------------------------- 激活/回滚事务性

def test_activate_load_failure_leaves_old_active(db, project_id, monkeypatch):
    a = _register_model(db, project_id)
    b = _register_model(db, project_id)
    monkeypatch.setattr(inference_service, "_load_model_runtime", _fake_loader)
    inference_service.activate_model(db, a.id)

    def boom(model):
        raise RuntimeError("artifact verify failed")

    monkeypatch.setattr(inference_service, "_load_model_runtime", boom)
    with pytest.raises(RuntimeError):
        inference_service.activate_model(db, b.id)

    db.expire_all()
    assert db.get(ModelVersion, b.id).status == ModelStatus.CANDIDATE
    assert db.get(ModelVersion, a.id).status == ModelStatus.ACTIVE
    assert db.get(Project, project_id).active_model_id == a.id
    assert inference_service.RUNTIME.get(project_id).model_version_id == a.id
    assert all(e.event == "model_activated" for e in inference_service.list_audit_events(db, project_id))
    assert len(inference_service.list_audit_events(db, project_id)) == 1


def test_rollback_load_failure_keeps_archived(db, project_id, monkeypatch):
    a = _register_model(db, project_id)
    b = _register_model(db, project_id)
    monkeypatch.setattr(inference_service, "_load_model_runtime", _fake_loader)
    inference_service.activate_model(db, a.id)
    inference_service.activate_model(db, b.id)

    def boom(model):
        raise RuntimeError("artifact verify failed")

    monkeypatch.setattr(inference_service, "_load_model_runtime", boom)
    with pytest.raises(RuntimeError):
        inference_service.rollback_model(db, a.id)

    db.expire_all()
    # a 保持 ARCHIVED——没有旧实现会留下的 CANDIDATE 中间态
    assert db.get(ModelVersion, a.id).status == ModelStatus.ARCHIVED
    assert db.get(ModelVersion, b.id).status == ModelStatus.ACTIVE
    assert db.get(Project, project_id).active_model_id == b.id
    assert inference_service.RUNTIME.get(project_id).model_version_id == b.id
    assert all(e.event != "model_rolled_back" for e in inference_service.list_audit_events(db, project_id))


def test_activate_and_rollback_write_audit_rows(db, project_id, monkeypatch):
    a = _register_model(db, project_id)
    b = _register_model(db, project_id)
    monkeypatch.setattr(inference_service, "_load_model_runtime", _fake_loader)

    inference_service.activate_model(db, a.id)
    inference_service.activate_model(db, b.id)
    inference_service.rollback_model(db, a.id)

    events = inference_service.list_audit_events(db, project_id)
    timeline = [(e.event, e.from_model_id, e.to_model_id) for e in reversed(events)]
    assert timeline == [
        ("model_activated", None, a.id),
        ("model_activated", a.id, b.id),
        ("model_rolled_back", b.id, a.id),
    ]
    db.expire_all()
    assert db.get(Project, project_id).active_model_id == a.id
    assert db.get(ModelVersion, a.id).status == ModelStatus.ACTIVE
    assert db.get(ModelVersion, b.id).status == ModelStatus.ARCHIVED
    assert inference_service.RUNTIME.get(project_id).model_version_id == a.id


def test_rollback_requires_archived_status(db, project_id, monkeypatch):
    a = _register_model(db, project_id)
    monkeypatch.setattr(inference_service, "_load_model_runtime", _fake_loader)
    with pytest.raises(ApiError) as exc:
        inference_service.rollback_model(db, a.id)
    assert exc.value.code == "MODEL_NOT_ROLLBACKABLE"


# ---------------------------------------------------------------- 互斥锁

def test_lifecycle_lock_mutual_exclusion(db, project_id, monkeypatch):
    a = _register_model(db, project_id)
    monkeypatch.setattr(inference_service, "_load_model_runtime", _fake_loader)

    entered = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []

    def slow_path():
        with run_service.project_lifecycle_lock(project_id):
            entered.set()
            release.wait(timeout=5)
        session = SessionLocal()
        try:
            inference_service.activate_model(session, a.id)
            outcomes.append("first-ok")
        except ApiError as exc:
            outcomes.append(f"first-{exc.code}")
        except Exception as exc:
            outcomes.append(f"first-{type(exc).__name__}")
        finally:
            session.close()

    def competing_path():
        session = SessionLocal()
        try:
            inference_service.activate_model(session, a.id)
            outcomes.append("second-ok")
        except ApiError as exc:
            outcomes.append(f"second-{exc.code}")
        finally:
            session.close()

    holder = threading.Thread(target=slow_path)
    holder.start()
    assert entered.wait(2)

    competitor = threading.Thread(target=competing_path)
    competitor.start()
    competitor.join(timeout=0.5)
    assert competitor.is_alive(), "生命周期操作应被项目互斥锁阻塞"

    release.set()
    competitor.join(timeout=5)
    holder.join(timeout=5)
    assert not competitor.is_alive() and not holder.is_alive()

    # 两次激活串行执行：恰好一成一败，败者读到对手已切换的状态
    oks = [o for o in outcomes if o.endswith("-ok")]
    assert len(oks) == 1
    loser_code = next(o for o in outcomes if not o.endswith("-ok")).split("-", 1)[1]
    assert loser_code in {"MODEL_NOT_ACTIVATABLE", "MODEL_ALREADY_ACTIVE"}
    db.expire_all()
    assert db.get(Project, project_id).active_model_id == a.id
    assert db.get(ModelVersion, a.id).status == ModelStatus.ACTIVE


# ---------------------------------------------------------------- API

def test_audit_events_endpoint(db, client, project_id, monkeypatch):
    a = _register_model(db, project_id)
    monkeypatch.setattr(inference_service, "_load_model_runtime", _fake_loader)

    resp = client.post(f"/api/v1/models/{a.id}/activate")
    assert resp.status_code == 200, resp.text
    resp = client.post(f"/api/v1/models/{a.id}/archive")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CANNOT_ARCHIVE_ACTIVE"

    resp = client.get(f"/api/v1/projects/{project_id}/audit-events")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items[0]["event"] == "model_activated"
    assert items[0]["to_model_id"] == a.id
    assert items[0]["from_model_id"] is None
    assert items[0]["created_at"]
