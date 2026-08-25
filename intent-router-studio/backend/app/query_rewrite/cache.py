"""改写缓存（修改方案 §12）。

- 键 = sha256(project_id | rewrite_config_version | terminology_version | prompt_version
            | normalized_query | normalized_context)
  缺任何一个版本维度都会串用不同配置的改写结果
- 只存结构化 RewriteResult + 安全检查摘要；不存 debug / request_id / 请求级延迟 / 人工编辑
- 进程内 LRU（默认 5000）+ TTL（默认 24h）；配置或模型切换按项目清空
- 生成模型由部署唯一决定（V2 §4.3 方案A），模型随进程重启而切换、缓存随进程
  重建，因此键不再包含 model_id——项目配置里的 model_id 从不参与生成，
  把它混进键只会造成无意义的缓存碎片
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any

from app.router_core.normalization import normalize_text

DEFAULT_CAPACITY = 5_000
DEFAULT_TTL_S = 24 * 3600


def build_cache_key(
    project_id: str,
    rewrite_config_version: str,
    terminology_version: str,
    prompt_version: str,
    query: str,
    context: str | None,
) -> str:
    payload = "|".join(
        (
            project_id,
            str(rewrite_config_version),
            str(terminology_version),
            str(prompt_version),
            normalize_text(query),
            normalize_text(context or ""),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RewriteCache:
    """线程安全 LRU + TTL；条目记录所属项目用于按项目失效。"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY, ttl_s: int = DEFAULT_TTL_S) -> None:
        self.capacity = capacity
        self.ttl_s = ttl_s
        self._data: OrderedDict[str, tuple[float, str, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            stored_at, _project, value = entry
            if now - stored_at > self.ttl_s:
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: str, project_id: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), project_id, value)
            self._data.move_to_end(key)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def clear_project(self, project_id: str) -> int:
        """配置 / 术语 / 模型切换时清空该项目的全部缓存条目。"""
        with self._lock:
            stale = [k for k, (_t, p, _v) in self._data.items() if p == project_id]
            for k in stale:
                del self._data[k]
            return len(stale)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
