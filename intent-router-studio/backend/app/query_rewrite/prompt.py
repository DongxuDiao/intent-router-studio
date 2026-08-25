"""Prompt 构造（修改方案 §8）：rewrite-prompt-v1 与 few-shot。

确定性结构任务：greedy decoding，不采样；输出必须是纯 JSON。
"""
from __future__ import annotations

import json

PROMPT_VERSION = "rewrite-prompt-v5"

SYSTEM_PROMPT = """你是 Query Rewrite Engine。你的任务是把用户本轮 Query 改写为脱离上下文后仍可理解的 Query。

必须遵守：
1. 只能使用 original_query 和 context 中明确存在的信息。
2. 不得创造 ID、名称、时间、数值、对象、动作或授权。
3. 不得将"如何做/能否做/想了解"改成执行命令。
4. 不得删除或反转否定、条件、犹豫、撤销、假设和不确定表达。
5. 指代无法唯一解析时，不猜测；保留原文并输出 missing_slots。
6. 补全指代或省略的对象时，只能使用 context 中逐字出现的信息。
7. 输出必须符合给定 JSON Schema，不输出解释性正文。
8. confidence 表示改写忠实度，不表示执行授权。
9. 只输出 standalone_query、confidence、rewrite_type、reason_codes 四个字段，禁止增加其他字段。
10. JSON 必须单行紧凑输出；闭合大括号后立即停止生成。"""

OUTPUT_SCHEMA_HINT = {
    "standalone_query": "改写后的 Query；无需改写则原样返回",
    "confidence": "0到1",
    "rewrite_type": "none|context_resolution|ellipsis_completion|mixed",
    "reason_codes": ["NO_REWRITE_NEEDED|RESOLVED_PRONOUN|COMPLETED_ELLIPSIS|AMBIGUOUS_REFERENCE"],
}

FEW_SHOTS: list[dict[str, str]] = [
    # 咨询不能变执行
    {
        "role": "user",
        "content": "context: 当前讨论实验 123\noriginal_query: 这个怎么停？",
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "standalone_query": "如何停止实验 123？",
                "rewrite_type": "context_resolution",
                "confidence": 0.95,
                "reason_codes": ["RESOLVED_PRONOUN"],
            },
            ensure_ascii=False,
        ),
    },
    # 无法解析时不猜
    {
        "role": "user",
        "content": "context: （空）\noriginal_query: 把那个关了",
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "standalone_query": "把那个关了",
                "rewrite_type": "none",
                "confidence": 0.4,
                "reason_codes": ["AMBIGUOUS_REFERENCE"],
            },
            ensure_ascii=False,
        ),
    },
    # 保留否定
    {
        "role": "user",
        "content": "context: 实验 123 正在运行\noriginal_query: 先别停它",
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "standalone_query": "暂时不要停止实验 123",
                "rewrite_type": "context_resolution",
                "confidence": 0.93,
                "reason_codes": ["RESOLVED_PRONOUN"],
            },
            ensure_ascii=False,
        ),
    },
]


def build_messages(
    original_query: str,
    context: str | None,
    terminology: dict[str, str] | None = None,
    max_context_chars: int = 4000,
) -> list[dict[str, str]]:
    """组装 chat messages：system（规则 + Schema）→ few-shot → 本轮输入。"""
    trimmed_context = (context or "").strip()
    if len(trimmed_context) > max_context_chars:
        trimmed_context = trimmed_context[:max_context_chars]

    term_hint = ""
    if terminology:
        pairs = "; ".join(f"{k} → {v}" for k, v in list(terminology.items())[:50])
        term_hint = f"\n术语归一表（改写时使用标准术语）：{pairs}"

    user_content = (
        f"context: {trimmed_context or '（空）'}\n"
        f"original_query: {original_query}"
        f"{term_hint}\n"
        "请只输出最小必要字段的单行 JSON，生成后立即结束。"
    )
    return [
        {
            "role": "system",
            "content": f"{SYSTEM_PROMPT}\n输出格式：{json.dumps(OUTPUT_SCHEMA_HINT, ensure_ascii=False, separators=(',', ':'))}",
        },
        *FEW_SHOTS,
        {"role": "user", "content": user_content},
    ]
