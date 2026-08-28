"""Provider 抽象（修改方案 §9.2）。

实现：qwen_provider（本地 Transformers）、stub（测试 / 离线演示）。
后续可扩展 local_mlx / llama_cpp / 远程兼容接口。
"""
from __future__ import annotations

import re
import time
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from app.query_rewrite.prompt import PROMPT_VERSION
from app.query_rewrite.schemas import ProviderOutput


class ProviderUnavailable(Exception):
    """Provider 不可用 / 熔断打开。"""

    # 远程 Provider 错误子类填充：降级原因码（透出到 fallback_reason）与重试语义
    fallback_code = "PROVIDER_UNAVAILABLE"
    retryable = False
    persistent = False  # 持久错误（鉴权/欠费）：连接应标记 unhealthy 直到修复
    retry_after_s: float | None = None


class ProviderTimeout(Exception):
    """生成超时。"""

    fallback_code = "TIMEOUT"


class ProviderBusy(Exception):
    """生成队列已满（V2 §3.3 有界准入拒绝）：调用方应立即回退原文，不应等待。"""

    fallback_code = "REWRITER_BUSY"


class ProviderBadRequest(ProviderUnavailable):
    """请求不合法（400 / 模型参数错误 / 业务码 1210~1215 等）：不重试。"""

    fallback_code = "PROVIDER_INVALID_REQUEST"


class ProviderAuthError(ProviderUnavailable):
    """鉴权失败（401/403 / 1000~1003 / 1220）：持久错误，连接标记异常。"""

    fallback_code = "PROVIDER_AUTH_FAILED"
    persistent = True


class ProviderRateLimited(ProviderUnavailable):
    """速率限制（429 / 1302 / 1305）：可重试一次，不计入故障熔断。"""

    fallback_code = "PROVIDER_RATE_LIMITED"
    retryable = True


class ProviderQuotaExceeded(ProviderUnavailable):
    """欠费 / 额度耗尽：持久错误，不重试。"""

    fallback_code = "PROVIDER_QUOTA_EXCEEDED"
    persistent = True


class ProviderUsage(BaseModel):
    """远程模型 token 用量（只进聚合指标，不落原文）。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ProviderReply(BaseModel):
    """rewriter 服务与 Provider 的统一返回（HTTP 与进程内同构）。"""

    output: ProviderOutput
    latency_ms: float
    provider: str
    model_id: str
    prompt_version: str
    # 外部 Provider V1：连接与观测元信息（本地 Qwen / stub 不填）
    request_id: str | None = None
    usage: ProviderUsage | None = None
    connection_id: str | None = None
    connection_revision: int | None = None


@runtime_checkable
class RewriteProvider(Protocol):
    provider_name: str
    model_id: str

    def health(self) -> dict[str, Any]: ...

    def rewrite(
        self,
        original_query: str,
        context: str | None,
        terminology: dict[str, str] | None = None,
        timeout_ms: int = 5000,
    ) -> ProviderReply: ...


# ---------------------------------------------------------------- stub

_PRONOUN_RE = re.compile(r"^(这个|那个|它|这|那)")
_ENTITY_RE = re.compile(r"(实验|任务|项目|服务|集群|指标)\s*([A-Za-z][A-Za-z0-9_-]*|\d+)")
_VERB_MAP = {"停": "停止", "删": "删除", "关": "关闭", "调": "调整", "查": "查询", "看": "查看"}


class StubProvider:
    """确定性规则改写：仅用于测试与离线演示，不加载模型。

    规则（可预测，便于断言）：
    - 上下文含可解析实体（如"实验 123"）且 Query 以指代词开头 → 实体替换指代 + 动词标准化
    - 其余情况原样返回（NO_REWRITE_NEEDED）
    - failure_mode="timeout"|"invalid_json"|"unavailable" 用于降级测试
    """

    provider_name = "stub"
    model_id = "stub-rewriter"

    def __init__(self, failure_mode: str | None = None) -> None:
        self.failure_mode = failure_mode

    def health(self) -> dict[str, Any]:
        return {
            "ok": self.failure_mode != "unavailable",
            "provider": self.provider_name,
            "model_id": self.model_id,
            "failure_mode": self.failure_mode,
        }

    def rewrite(
        self,
        original_query: str,
        context: str | None,
        terminology: dict[str, str] | None = None,
        timeout_ms: int = 5000,
    ) -> ProviderReply:
        start = time.perf_counter()
        if self.failure_mode == "unavailable":
            raise ProviderUnavailable("stub 配置为不可用")
        if self.failure_mode == "timeout":
            raise ProviderTimeout("stub 配置为超时")
        if self.failure_mode == "busy":
            raise ProviderBusy("stub 配置为队列已满")
        if self.failure_mode == "invalid_json":
            return ProviderReply(
                output=ProviderOutput(standalone_query=original_query, confidence=0.0, rewrite_type="none"),
                latency_ms=round((time.perf_counter() - start) * 1000, 2),
                provider=self.provider_name,
                model_id=self.model_id,
                prompt_version=PROMPT_VERSION,
            )

        text = original_query
        entity_match = _ENTITY_RE.search(context or "")
        pronoun = _PRONOUN_RE.match(text)
        reason_codes: list[str] = []
        rewrite_type = "none"
        changed = False
        if entity_match and pronoun:
            entity = f"{entity_match.group(1)} {entity_match.group(2)}"
            rest = text[pronoun.end():]
            for key, verb in _VERB_MAP.items():
                if rest.startswith(key):
                    rest = verb + rest[len(key):]
                    break
            text = f"{rest} {entity}" if not rest.endswith(entity) else rest
            changed = text != original_query
            rewrite_type = "context_resolution"
            reason_codes = ["RESOLVED_PRONOUN"]

        latency = round((time.perf_counter() - start) * 1000, 2)
        output = ProviderOutput(
            standalone_query=text,
            rewrite_type=rewrite_type,  # type: ignore[arg-type]
            should_use=changed,
            confidence=0.9 if changed else 0.5,
            preserved_intent=True,
            reason_codes=reason_codes or ["NO_REWRITE_NEEDED"],
        )
        return ProviderReply(
            output=output,
            latency_ms=latency,
            provider=self.provider_name,
            model_id=self.model_id,
            prompt_version=PROMPT_VERSION,
        )
