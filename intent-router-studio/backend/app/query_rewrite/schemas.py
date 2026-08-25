"""改写输出协议（修改方案 §6）。

- Provider 只需输出核心生成字段（standalone_query / confidence / 结构化理解），
  协议字段（original_query / normalized_query / model / latency）由服务端组装
- 所有 Provider 输出必须经 pydantic 校验，非法字段拒绝并回退原文（INVALID_JSON）
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# §6.2 枚举
REWRITE_TYPES = (
    "none",
    "normalization",
    "term_normalization",
    "context_resolution",
    "ellipsis_completion",
    "mixed",
)

REASON_CODES = (
    "NO_REWRITE_NEEDED",
    "NORMALIZED_TERM",
    "RESOLVED_PRONOUN",
    "COMPLETED_ELLIPSIS",
    "MISSING_CONTEXT",
    "AMBIGUOUS_REFERENCE",
    "UNSUPPORTED_ASSUMPTION",
    "NEGATION_CHANGED",
    "MODALITY_CHANGED",
    "ACTION_INTENSIFIED",
    "OBJECT_INVENTED",
    "ROUTE_CONFLICT",
    "LOW_CONFIDENCE",
    "TIMEOUT",
    "INVALID_JSON",
    "PROVIDER_UNAVAILABLE",
    "REWRITER_BUSY",
)

RewriteType = Literal[
    "none",
    "normalization",
    "term_normalization",
    "context_resolution",
    "ellipsis_completion",
    "mixed",
]

MAX_STANDALONE_CHARS = 500


class ProviderObject(BaseModel):
    """§3.2 objects：文本中明确出现或可由上下文唯一解析的对象。"""

    model_config = ConfigDict(extra="ignore")

    type: str = Field(default="entity", max_length=50)
    value: str = Field(max_length=200)
    source: Literal["query", "context", "terminology"] = "query"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ProviderOutput(BaseModel):
    """Provider 生成核心字段（§6.1 中由模型负责的部分）。

    extra="ignore"：未知字段丢弃而非整包拒绝（模型偶发附加字段不致命）；
    类型不符 / 缺必填字段仍拒绝。
    """

    model_config = ConfigDict(extra="ignore")

    standalone_query: str = Field(max_length=MAX_STANDALONE_CHARS)
    rewrite_type: RewriteType = "none"
    should_use: bool = True
    confidence: float = Field(ge=0.0, le=1.0)
    preserved_intent: bool = True
    mentioned_action: str | None = Field(default=None, max_length=200)
    objects: list[ProviderObject] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=10)
    used_context_refs: list[str] = Field(default_factory=list, max_length=20)
    reason_codes: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("reason_codes")
    @classmethod
    def _known_reason_codes(cls, v: list[str]) -> list[str]:
        unknown = [c for c in v if c not in REASON_CODES]
        if unknown:
            raise ValueError(f"未知 reason_codes: {unknown[:3]}")
        return v

    @field_validator("missing_slots", "assumptions", "used_context_refs")
    @classmethod
    def _str_items(cls, v: list[Any]) -> list[str]:
        for item in v:
            if not isinstance(item, str):
                raise ValueError("列表元素必须是字符串")
        return v

    @field_validator("standalone_query")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("standalone_query 不能为空")
        return v


class RewriteModelInfo(BaseModel):
    provider: str = "stub"
    model_id: str = "stub"
    prompt_version: str = "rewrite-prompt-v1"


class RewriteResult(BaseModel):
    """§6.1 完整 RewriteResult（服务端组装后对外暴露）。"""

    model_config = ConfigDict(extra="allow")

    original_query: str
    normalized_query: str
    standalone_query: str
    rewrite_type: RewriteType
    changed: bool
    should_use: bool
    confidence: float = Field(ge=0.0, le=1.0)
    preserved_intent: bool
    mentioned_action: str | None = None
    objects: list[ProviderObject] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    used_context_refs: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    model: RewriteModelInfo = Field(default_factory=RewriteModelInfo)
    latency_ms: float = 0.0

    # L0 术语归一 trace（§3.1：每次替换记录 source_span / target_term / rule_id）
    term_replacements: list[dict[str, Any]] = Field(default_factory=list)


class RewriteParseError(ValueError):
    """Provider 输出无法解析为合法 JSON / 校验失败。"""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def extract_json_block(text: str) -> str:
    """从模型原始输出提取 JSON：优先 ```json 围栏，否则从首个 { 起做括号平衡。"""
    text = (text or "").strip()
    if not text:
        raise RewriteParseError("空输出")
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise RewriteParseError("输出中未找到 JSON 对象")
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise RewriteParseError("JSON 大括号不平衡")


def parse_provider_output(raw: str) -> ProviderOutput:
    """原始生成文本 → 校验后的 ProviderOutput；失败抛 RewriteParseError。"""
    try:
        data = json.loads(extract_json_block(raw))
    except json.JSONDecodeError as exc:
        raise RewriteParseError(f"非法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RewriteParseError("JSON 根节点必须是对象")
    try:
        return ProviderOutput.model_validate(data)
    except Exception as exc:  # 统一转译
        raise RewriteParseError(f"输出校验失败: {exc}") from exc
