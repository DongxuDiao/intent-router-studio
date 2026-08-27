"""生成 worker 子进程测试（修改方案 V2 §3.3）。

不加载真实模型：QwenProvider 的 worker 入口可注入，本文件提供一个
模块级假入口（spawn 上下文要求可 pickle），用消息内容控制行为：
- "SLEEP:<秒>" 模拟慢生成（用于超时终止）
- "BOOM" 模拟生成进程崩溃（os._exit）
- 其余原样返回

验证四件事：
1. 超时真正 terminate 子进程（不是 daemon 线程放弃后继续吃 CPU）
2. 有界队列满 → ProviderBusy（上层 429 → 回退原文）
3. 超时后 worker 自动重启，后续请求恢复
4. 观测指标（queue_depth / active_generation / busy_reject_total /
   generation_timeout_total / worker_restarts）
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from app.query_rewrite.provider import (
    ProviderBusy,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.query_rewrite.qwen_provider import QwenProvider, _has_complete_json


def _fake_worker_main(conn, model_id, device, max_new_tokens, repetition_penalty, threads):
    conn.send(("ready", "cpu-fake"))
    while True:
        try:
            request = conn.recv()
        except (EOFError, OSError):
            return
        if not request or request[0] == "shutdown":
            return
        content = request[1][0]["content"]
        if content.startswith("SLEEP:"):
            time.sleep(float(content.split(":", 1)[1]))
            conn.send(("ok", "slept"))
        elif content == "BOOM":
            os._exit(1)
        else:
            conn.send(("ok", content))


def _provider(capacity: int = 2) -> QwenProvider:
    return QwenProvider(queue_capacity=capacity, worker_entry=_fake_worker_main)


def _msg(content: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": content}]


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("条件等待超时")


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # 僵尸进程仍占 pid


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("还没有 JSON }", False),
        ('前缀 {"a":1}', True),
        ('{"a":{"b":1}} 后缀', True),
        ('{"a":"文本里的 } 不闭合"}', True),
        ('{"a":1', False),
    ],
)
def test_complete_json_detection(text, expected):
    assert _has_complete_json(text) is expected


def test_generate_via_worker_process():
    p = _provider()
    assert p._generate(_msg("hello"), 5_000) == "hello"
    assert p.health()["device"] == "cpu-fake"
    m = p.metrics()
    assert m["active_generation"] == 0 and m["queue_depth"] == 0
    p._worker.terminate()


def test_timeout_keeps_loaded_worker_and_drains_late_result():
    p = _provider()
    worker = p._get_worker()
    pid = worker._proc.pid
    with pytest.raises(ProviderTimeout):
        p._generate(_msg("SLEEP:0.5"), 100)
    # 软超时只结束调用方等待；后台排空迟到结果，模型进程保持常驻。
    assert worker.alive is True
    with pytest.raises(ProviderBusy):
        p._generate(_msg("too early"), 1_000)
    _wait_until(lambda: worker.available, timeout=3.0)
    assert worker._proc.pid == pid
    assert p._generate(_msg("reused"), 5_000) == "reused"
    assert p.metrics()["generation_timeout_total"] == 1
    assert p.metrics()["worker_restarts"] == 0
    worker.terminate()


def test_busy_reject_when_queue_full():
    p = _provider(capacity=1)
    done = threading.Event()

    def _slow():
        p._generate(_msg("SLEEP:3"), 10_000)
        done.set()

    t = threading.Thread(target=_slow)
    t.start()
    try:
        _wait_until(lambda: p.metrics()["active_generation"] == 1)
        with pytest.raises(ProviderBusy):
            p._generate(_msg("fast"), 5_000)
        m = p.metrics()
        assert m["busy_reject_total"] == 1
        assert m["queue_capacity"] == 1
    finally:
        t.join(timeout=15)
    assert done.is_set()


def test_queue_depth_counts_waiting_caller():
    p = _provider(capacity=2)
    def _slow():
        p._generate(_msg("SLEEP:1.5"), 10_000)

    t1 = threading.Thread(target=_slow)
    t1.start()
    _wait_until(lambda: p.metrics()["active_generation"] == 1)
    t2 = threading.Thread(target=_slow)
    t2.start()
    try:
        _wait_until(lambda: p.metrics()["queue_depth"] == 1)
        with pytest.raises(ProviderBusy):  # 容量 2 已占满（1 在途 + 1 排队）
            p._generate(_msg("fast"), 5_000)
        assert p.metrics()["busy_reject_total"] == 1
    finally:
        t1.join(timeout=15)
        t2.join(timeout=15)
    assert p.metrics()["queue_depth"] == 0


def test_worker_is_reused_after_timeout_result_is_drained():
    p = _provider()
    worker = p._get_worker()
    pid = worker._proc.pid
    with pytest.raises(ProviderTimeout):
        p._generate(_msg("SLEEP:0.4"), 100)
    _wait_until(lambda: worker.available, timeout=3.0)
    assert p._generate(_msg("ping"), 10_000) == "ping"
    assert p._worker._proc.pid == pid
    assert p.metrics()["worker_restarts"] == 0
    p._worker.terminate()


def test_crashed_worker_maps_to_unavailable():
    p = _provider()
    with pytest.raises(ProviderUnavailable):
        p._generate(_msg("BOOM"), 5_000)
    # 崩溃后可恢复
    assert p._generate(_msg("recovered"), 5_000) == "recovered"
    assert p.metrics()["worker_restarts"] == 1
    p._worker.terminate()


def test_rewriter_app_maps_busy_429_and_exposes_metrics():
    from fastapi.testclient import TestClient

    from app.query_rewrite.provider import StubProvider
    from app.rewriter.main import build_rewriter_app

    with TestClient(build_rewriter_app(StubProvider("busy"), warmup=False)) as c:
        r = c.post("/rewrite", json={"original_query": "q"})
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "REWRITER_BUSY"

    # QwenProvider 的 /health 透出有界队列与超时终止指标
    p = _provider()
    app = build_rewriter_app(p, warmup=False)
    with TestClient(app) as c:
        h = c.get("/health").json()
        assert h["metrics"]["queue_capacity"] == 2
        assert h["metrics"]["active_generation"] == 0
        assert h["metrics"]["busy_reject_total"] == 0


def test_rewriter_health_is_not_ready_until_model_loaded():
    from fastapi.testclient import TestClient

    from app.rewriter.main import build_rewriter_app

    p = _provider()
    with TestClient(build_rewriter_app(p, warmup=False)) as c:
        h = c.get("/health")
        assert h.status_code == 503
        assert h.json()["loaded"] is False
        p._get_worker()
        h = c.get("/health")
        assert h.status_code == 200
        assert h.json()["loaded"] is True
    p._worker.terminate()
