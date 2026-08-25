"""推理服务：ACTIVE 模型解析、显式版本加载、激活切换、Playground 反馈。"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app import ids
from app.config import get_settings
from app.constants import ModelStatus
from app.errors import ApiError, NotFoundError
from app.models import ModelVersion, PlaygroundCase, Project
from app.router_core.normalization import text_hash
from app.router_core.runtime import InferenceRuntime, ModelRuntime
from app.services import run_service

RUNTIME = InferenceRuntime()


def _load_model_runtime(model: ModelVersion) -> ModelRuntime:
    return ModelRuntime.load(model.artifact_path, model.id, verify=True)


def ensure_project_runtime(db: Session, project_id: str) -> ModelRuntime:
    """获取（必要时加载）项目 ACTIVE 模型；无 ACTIVE 模型报 MODEL_NOT_ACTIVE。

    V2 §3.5：缓存命中也校验 runtime.model_version_id 与 project.active_model_id
    一致——指针已被其他路径（激活/回滚/直改 DB）更换时，弃用陈旧缓存重载，
    绝不用旧模型回答新指针下的请求。
    """
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project", project_id)
    active_model_id = project.active_model_id

    runtime = RUNTIME.get(project_id)
    if runtime is not None and runtime.model_version_id == active_model_id:
        return runtime
    if runtime is not None:
        RUNTIME.evict(project_id)

    model = db.get(ModelVersion, active_model_id) if active_model_id else None
    if model is None or model.status != ModelStatus.ACTIVE:
        raise ApiError("MODEL_NOT_ACTIVE", "项目没有激活模型，请先在模型注册中心激活", 409)
    runtime = _load_model_runtime(model)
    RUNTIME.set(project_id, runtime)
    return runtime


def ensure_version_runtime(db: Session, model_version_id: str) -> ModelRuntime:
    runtime = RUNTIME.get_version(model_version_id)
    if runtime is not None:
        return runtime
    model = run_service.get_model(db, model_version_id)
    runtime = _load_model_runtime(model)
    RUNTIME.set_version(model_version_id, runtime)
    return runtime


def predict(
    db: Session,
    project_id: str,
    text: str,
    context: str | None = None,
    model_version_id: str | None = None,
    threshold_overrides: dict | None = None,
    debug: bool = False,
    rewrite_options: dict | None = None,
) -> dict:
    _validate_text(text, context)
    if model_version_id:
        model = run_service.get_model(db, model_version_id)
        if model.project_id != project_id:
            raise ApiError("VALIDATION_ERROR", "模型不属于该项目", 422)
        runtime = ensure_version_runtime(db, model_version_id)
    else:
        runtime = ensure_project_runtime(db, project_id)
    result = RUNTIME.predict_with(runtime, text, context, threshold_overrides, debug)
    if rewrite_options and rewrite_options.get("enabled"):
        result = _attach_query_understanding(
            db, project_id, text, context, runtime, result, rewrite_options
        )
    return result


def _attach_query_understanding(
    db: Session,
    project_id: str,
    text: str,
    context: str | None,
    runtime: ModelRuntime,
    result: dict,
    rewrite_options: dict,
) -> dict:
    """把改写理解结果挂到 /predict 响应（§6.3 query_understanding）。

    不变量（§9.4 / §20）：改写链路任何失败都不影响主响应；
    正式路由字段（route/decision/...）永远来自原文预测，此处只新增字段。
    """
    from app.services import rewrite_service  # 延迟导入避免循环依赖

    include_trace = bool(rewrite_options.get("include_trace"))
    # 原文路由必须快照：understanding.original_route 若直接引用 result 本体，
    # 会与 result["query_understanding"] 形成循环引用，序列化失败
    snapshot = dict(result)
    try:
        understanding = rewrite_service.understand_query(
            db,
            project_id,
            text,
            context,
            runtime,
            mode_override=rewrite_options.get("mode") or None,
            predict_fn=lambda t, c: (
                snapshot if (t == text and c == context) else RUNTIME.predict_with(runtime, t, c)
            ),
        )
    except Exception as exc:  # 改写链路故障绝不影响 /predict 主响应
        logging.getLogger("app.rewrite").warning(
            "query_understanding 降级 project=%s hash=%s err=%s",
            project_id,
            text_hash(text, context)[:12],
            f"{type(exc).__name__}: {exc}"[:200],
        )
        result["query_understanding"] = {
            "available": False,
            "fallback_reason": "PROVIDER_UNAVAILABLE",
            "downstream_query": text,
            "downstream_query_source": "original",
        }
        return result
    if include_trace:
        result["query_understanding"] = {"available": True, **understanding}
    else:
        result["query_understanding"] = {
            "available": True,
            "mode": understanding["mode"],
            "downstream_query": understanding["downstream_query"],
            "downstream_query_source": understanding["downstream_query_source"],
            "safety_decision": understanding["safety_decision"],
            "route_consistent": understanding["route_consistent"],
            "fallback_reason": understanding.get("fallback_reason"),
        }
    return result


def predict_batch(db: Session, project_id: str, items: list[dict]) -> list[dict]:
    settings = get_settings()
    if len(items) > settings.max_batch_inference:
        raise ApiError(
            "BATCH_TOO_LARGE",
            f"批量推理上限 {settings.max_batch_inference} 条",
            422,
            {"limit": settings.max_batch_inference},
        )
    results = []
    for item in items:
        text = item.get("text", "")
        context = item.get("context")
        _validate_text(text, context)
        model_version_id = item.get("model_version_id")
        overrides = item.get("threshold_overrides")
        if model_version_id or overrides:
            results.append(predict(db, project_id, text, context, model_version_id, overrides))
        else:
            results.append(predict(db, project_id, text, context))
    return results


def compare(db: Session, project_id: str, text: str, context: str | None, model_a: str | None, model_b: str) -> dict:
    _validate_text(text, context)
    result_a = predict(db, project_id, text, context, model_a)
    result_b = predict(db, project_id, text, context, model_b)
    diff = {
        "route_differs": result_a["route"] != result_b["route"],
        "decision_differs": result_a["decision"] != result_b["decision"],
        "margin_diff": round(abs(result_a["margin"] - result_b["margin"]), 6),
    }
    return {"a": result_a, "b": result_b, "diff": diff}


def activate_model(db: Session, model_id: str) -> ModelVersion:
    """激活流程：项目互斥锁 → 加载（含制品完整性校验与 smoke）→ 单事务切换 → 原子替换运行时引用。

    _load_model_runtime 内部执行 verify_manifest（覆盖普通制品与模型权重），
    任何校验/加载失败都在状态切换前抛出，旧 ACTIVE 模型不受影响。
    """
    model = run_service.get_model(db, model_id)
    if model.status not in (ModelStatus.CANDIDATE, ModelStatus.VALIDATED):
        raise ApiError("MODEL_NOT_ACTIVATABLE", f"模型状态 {model.status} 不可激活", 409)

    with run_service.project_lifecycle_lock(model.project_id):
        candidate_runtime = _load_model_runtime(model)  # verify=True + smoke inference

        project = db.get(Project, model.project_id)
        previous_active_id = project.active_model_id if project else None
        if previous_active_id == model_id:
            raise ApiError("MODEL_ALREADY_ACTIVE", "该模型已是 ACTIVE", 409)

        # 单事务：旧 ACTIVE → ARCHIVED、目标 → ACTIVE、项目指针、审计事件
        if previous_active_id:
            previous = db.get(ModelVersion, previous_active_id)
            if previous is not None:
                previous.status = ModelStatus.ARCHIVED
        model.status = ModelStatus.ACTIVE
        model.activated_at = datetime.now(UTC)
        if project is not None:
            project.active_model_id = model_id
        run_service.record_audit(
            db,
            model.project_id,
            "model_activated",
            from_model_id=previous_active_id,
            to_model_id=model_id,
        )
        db.commit()

        RUNTIME.set(model.project_id, candidate_runtime)
        db.refresh(model)
        return model


def rollback_model(db: Session, model_id: str) -> ModelVersion:
    """回滚 = 重新激活一个已归档的模型（V2 §3.5：先加载冒烟，再单事务切换）。

    旧实现先把状态改成 CANDIDATE 并提交、再走激活；加载失败会留下一个
    既不是 ARCHIVED 也不是 ACTIVE 的中间态。现在整个切换只在一个事务里发生。
    """
    model = run_service.get_model(db, model_id)
    with run_service.project_lifecycle_lock(model.project_id):
        if model.status != ModelStatus.ARCHIVED:
            raise ApiError("MODEL_NOT_ROLLBACKABLE", f"仅 ARCHIVED 模型可回滚，当前 {model.status}", 409)

        candidate_runtime = _load_model_runtime(model)  # 加载失败时模型保持 ARCHIVED

        project = db.get(Project, model.project_id)
        previous_active_id = project.active_model_id if project else None
        if previous_active_id == model_id:
            raise ApiError("MODEL_ALREADY_ACTIVE", "该模型已是 ACTIVE", 409)

        if previous_active_id:
            previous = db.get(ModelVersion, previous_active_id)
            if previous is not None:
                previous.status = ModelStatus.ARCHIVED
        model.status = ModelStatus.ACTIVE
        model.activated_at = datetime.now(UTC)
        if project is not None:
            project.active_model_id = model_id
        run_service.record_audit(
            db,
            model.project_id,
            "model_rolled_back",
            from_model_id=previous_active_id,
            to_model_id=model_id,
        )
        db.commit()

        RUNTIME.set(model.project_id, candidate_runtime)
        db.refresh(model)
        return model


def list_audit_events(db: Session, project_id: str, limit: int = 100) -> list:
    from app.models import AuditEvent

    return (
        db.query(AuditEvent)
        .filter(AuditEvent.project_id == project_id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
        .all()
    )


def _validate_text(text: str, context: str | None) -> None:
    settings = get_settings()
    if not text or not str(text).strip():
        raise ApiError("VALIDATION_ERROR", "text 不能为空", 422)
    if len(text) > settings.max_text_chars:
        raise ApiError("TEXT_TOO_LONG", f"text 超过 {settings.max_text_chars} 字符", 422)
    if context and len(context) > settings.max_text_chars:
        raise ApiError("TEXT_TOO_LONG", f"context 超过 {settings.max_text_chars} 字符", 422)


# ---------------------------------------------------------------- playground
def save_playground_case(
    db: Session,
    project_id: str,
    text: str,
    context: str | None,
    expected_label: str | None,
    predicted_route: str | None,
    model_version_id: str | None,
    tags: dict | None,
    save_text: bool,
) -> PlaygroundCase:
    case = PlaygroundCase(
        id=ids.prefixed(ids.CASE),
        project_id=project_id,
        text_hash=text_hash(text, context),
        text=text if save_text else None,
        context=context if save_text else None,
        expected_label=expected_label,
        predicted_route=predicted_route,
        model_version_id=model_version_id,
        tags=tags or {},
        is_correct=(expected_label == predicted_route) if expected_label else None,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


def list_playground_cases(db: Session, project_id: str, limit: int = 100) -> list[PlaygroundCase]:
    return (
        db.query(PlaygroundCase)
        .filter(PlaygroundCase.project_id == project_id)
        .order_by(PlaygroundCase.created_at.desc())
        .limit(limit)
        .all()
    )


def clear_playground_history(db: Session, project_id: str | None = None) -> int:
    query = db.query(PlaygroundCase)
    if project_id:
        query = query.filter(PlaygroundCase.project_id == project_id)
    count = query.count()
    query.delete(synchronize_session=False)
    db.commit()
    return count
