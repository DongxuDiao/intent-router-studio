"""改写缓存测试（修改方案 §12 / §16.1：版本隔离 + debug 不入缓存）。"""
from __future__ import annotations

import time

from app.query_rewrite.cache import RewriteCache, build_cache_key


def _key(project="prj_1", config="rcv_1", term="tv_1", prompt="rewrite-prompt-v1",
         q="这个怎么停", ctx="实验 123") -> str:
    return build_cache_key(project, config, term, prompt, q, ctx)


def test_put_get_roundtrip():
    cache = RewriteCache()
    cache.put(_key(), "prj_1", {"standalone_query": "如何停止实验 123？"})
    assert cache.get(_key())["standalone_query"] == "如何停止实验 123？"


def test_different_config_version_isolated():
    cache = RewriteCache()
    cache.put(_key(), "prj_1", {"v": "config-1"})
    assert cache.get(_key(config="rcv_2")) is None
    assert cache.get(_key())["v"] == "config-1"


def test_different_terminology_version_isolated():
    cache = RewriteCache()
    cache.put(_key(), "prj_1", {"v": "term-1"})
    assert cache.get(_key(term="tv_2")) is None


def test_prompt_version_isolated():
    """V2 §4.3 方案A 后键不再含 model_id（模型由部署唯一决定，随进程切换）。

    版本维度仍然齐全：prompt 版本不同即隔离。
    """
    cache = RewriteCache()
    cache.put(_key(), "prj_1", {"v": "v1"})
    assert cache.get(_key(prompt="rewrite-prompt-v3")) is None
    assert cache.get(_key())["v"] == "v1"


def test_different_project_isolated():
    cache = RewriteCache()
    cache.put(_key(), "prj_1", {"v": 1})
    assert cache.get(_key(project="prj_2")) is None


def test_normalized_text_and_context_keying():
    cache = RewriteCache()
    cache.put(_key(q="这个怎么停？", ctx="实验 123"), "prj_1", {"v": 1})
    # 全角/空白差异应命中同一键
    assert cache.get(_key(q=" 这个怎么停？ ", ctx="实验  123")) is not None
    # 上下文不同则不命中
    assert cache.get(_key(ctx="实验 456")) is None


def test_ttl_expiry(monkeypatch):
    cache = RewriteCache(ttl_s=1)
    cache.put(_key(), "prj_1", {"v": 1})
    assert cache.get(_key()) is not None
    # 快进单调时钟
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 2)
    assert cache.get(_key()) is None


def test_lru_eviction():
    cache = RewriteCache(capacity=3)
    keys = [_key(q=f"q{i}") for i in range(4)]
    for i, k in enumerate(keys):
        cache.put(k, "prj_1", {"i": i})
    assert len(cache) == 3
    assert cache.get(keys[0]) is None  # 最先插入被淘汰
    assert cache.get(keys[3])["i"] == 3


def test_clear_project_only_removes_that_project():
    cache = RewriteCache()
    cache.put(_key(), "prj_1", {"v": 1})
    cache.put(_key(project="prj_2"), "prj_2", {"v": 2})
    removed = cache.clear_project("prj_1")
    assert removed == 1
    assert cache.get(_key()) is None
    assert cache.get(_key(project="prj_2")) is not None


def test_debug_fields_not_stored_by_convention():
    """缓存值由服务层构造；此处固化约定：存入内容不含请求级字段。"""
    cache = RewriteCache()
    value = {"standalone_query": "如何停止实验 123？", "confidence": 0.9, "reason_codes": []}
    cache.put(_key(), "prj_1", value)
    assert "debug" not in cache.get(_key())
    assert "request_id" not in cache.get(_key())
    assert "latency_ms" not in cache.get(_key())
