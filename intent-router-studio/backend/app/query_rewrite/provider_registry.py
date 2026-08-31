"""Provider 注册表（外部模型 API 接入 V1 §5.3）。

- `builtin:local_qwen` 返回进程启动时的本地 Provider 单例（含预热）
- 远程连接按 (connection_id, revision) 构造并缓存 Provider；配置更新后
  revision 自增，新请求得到新实例，旧实例关闭连接池后淘汰
- 缓存上限 20：防反复建连泄漏
- 远程 Provider 不做启动预热；健康检查不发收费请求，连接正确性由显式
  「测试连接」验证
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

from app.query_rewrite.provider import ProviderUnavailable, RewriteProvider
from app.services.provider_connection_service import BUILTIN_LOCAL_QWEN

logger = logging.getLogger("app.provider_registry")

MAX_CACHED_PROVIDERS = 20


class ProviderRegistry:
    def __init__(self, builtin_provider: RewriteProvider) -> None:
        self._builtin = builtin_provider
        self._cache: OrderedDict[tuple[str, int], Any] = OrderedDict()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 解析
    def resolve(self, connection_id: str | None, expected_revision: int | None = None) -> RewriteProvider:
        """按连接 ID 解析 Provider；expected_revision 仅用于观测日志（以数据库
        当前 revision 为准——连接更新与请求并发时，缓存键取实际使用的 revision）。"""
        if not connection_id or connection_id == BUILTIN_LOCAL_QWEN:
            return self._builtin
        cache_key, provider = self._resolve_remote(connection_id)
        if expected_revision is not None and provider.connection_revision != expected_revision:
            logger.info(
                "provider_connection_revision_changed id=%s expected=%s actual=%s",
                connection_id, expected_revision, provider.connection_revision,
            )
        return provider

    def _resolve_remote(self, connection_id: str) -> tuple[tuple[str, int], Any]:
        # 每次解析都读一次连接行（主键查询，SQLite 微秒级）。连接的更新/
        # 删除/禁用只在处理该请求的进程内触发 listener，rewriter 进程无法
        # 跨进程收到通知——必须以数据库当前 revision 为准发现变化，
        # 否则会一直用旧 Key/旧端点的缓存实例（连接测试走 API 进程新建
        # Provider 会通过，业务改写却持续失败）。
        from app.db import SessionLocal
        from app.services import provider_connection_service as svc

        db = SessionLocal()
        try:
            row = db.get(svc.RewriteProviderConnection, connection_id)
            if row is None:
                self.invalidate(connection_id)
                raise ProviderUnavailable(f"连接不存在: {connection_id}")
            if not row.enabled:
                self.invalidate(connection_id)
                raise ProviderUnavailable(f"连接已禁用: {connection_id}")
            if not row.api_key_ciphertext:
                self.invalidate(connection_id)
                raise ProviderUnavailable(f"连接没有 API Key: {connection_id}")
            key = (connection_id, row.revision)
            with self._lock:
                # 构造发生在锁外：本线程读到旧 revision 后，另一线程可能已
                # 缓存更新 revision。任何情况下都不能让旧实例反向淘汰新实例。
                newer = [(k, p) for k, p in self._cache.items() if k[0] == connection_id and k[1] > key[1]]
                if newer:
                    newest_key, newest = max(newer, key=lambda item: item[0][1])
                    return newest_key, newest
                cached = self._cache.get(key)
                if cached is not None:
                    # 只淘汰更旧 revision；绝不关闭并发产生的更新 revision。
                    for stale in [k for k in self._cache if k[0] == connection_id and k[1] < key[1]]:
                        self._close_quietly(self._cache.pop(stale))
                    return key, cached
            # 构造（解密）在锁外进行，锁内只做缓存写
            provider = svc._build_remote_provider(row)
            with self._lock:
                newer = [(k, p) for k, p in self._cache.items() if k[0] == connection_id and k[1] > key[1]]
                if newer:
                    newest_key, newest = max(newer, key=lambda item: item[0][1])
                    self._close_quietly(provider)
                    return newest_key, newest
                raced = self._cache.get(key)
                if raced is not None:
                    self._close_quietly(provider)
                    return key, raced
                self._cache[key] = provider
                for stale in [k for k in self._cache if k[0] == connection_id and k[1] < key[1]]:
                    self._close_quietly(self._cache.pop(stale))
                while len(self._cache) > MAX_CACHED_PROVIDERS:
                    _, evicted = self._cache.popitem(last=False)
                    self._close_quietly(evicted)
                return key, provider
        finally:
            db.close()

    # ---------------------------------------------------------------- 失效
    def invalidate(self, connection_id: str) -> int:
        """连接更新/删除后淘汰其全部 revision 实例（关闭连接池）。"""
        evicted = 0
        with self._lock:
            for key in [k for k in self._cache if k[0] == connection_id]:
                self._close_quietly(self._cache.pop(key))
                evicted += 1
        return evicted

    def _close_quietly(self, provider: Any) -> None:
        close = getattr(provider, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:
            logger.exception("关闭 Provider 连接池失败")

    # ---------------------------------------------------------------- 观测
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "builtin": getattr(self._builtin, "provider_name", "?"),
                "cached_remote_providers": [
                    {"connection_id": cid, "revision": rev} for (cid, rev) in self._cache.keys()
                ],
            }


# ---------------------------------------------------------------- 进程级单例（rewriter 服务）

_REGISTRY: ProviderRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_registry() -> ProviderRegistry:
    """rewriter 进程内共享的注册表；内置 Provider 来自部署环境。"""
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                from app.rewriter.main import _build_provider_from_env

                _REGISTRY = ProviderRegistry(_build_provider_from_env())
                _install_listener(_REGISTRY)
    return _REGISTRY


def set_registry(registry: ProviderRegistry) -> None:
    """测试注入；同时挂接连接变更回调。"""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = registry
    _install_listener(registry)


def reset_registry() -> None:
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


def _install_listener(registry: ProviderRegistry) -> None:
    from app.services import provider_connection_service as svc

    def _on_change(event: str, row) -> None:
        # 连接更新/删除：淘汰旧 revision 实例（新 revision 惰性重建）
        if event == "changed" and row is not None:
            registry.invalidate(row.id)
        elif event == "changed" and row is None:
            with registry._lock:
                keys = [k for k in registry._cache]
            for cid in {k[0] for k in keys}:
                registry.invalidate(cid)
        # test_ok/test_failed：实例与熔断由各进程自行维护，无需处理

    svc.register_listener(_on_change)
