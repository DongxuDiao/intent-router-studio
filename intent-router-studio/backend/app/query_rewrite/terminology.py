"""L0 项目术语归一（修改方案 §3.1 / §11.2）。

- 别名 → 标准术语；明确拼写纠正；不改变动作与否定关系
- 每次替换记录 source_span / target_term / rule_id（trace）
- never_replace_when 命中则跳过该次替换；纯 ASCII 别名用词边界，避免 exp 命中 expand
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from pydantic import BaseModel, Field


class TermRule(BaseModel):
    canonical: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    confusable_with: list[str] = Field(default_factory=list, max_length=50)
    never_replace_when: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool = True

    @property
    def rule_id(self) -> str:
        payload = f"{self.canonical}|{'|'.join(sorted(self.aliases))}"
        return f"term-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:10]}"


class TermApplyResult(BaseModel):
    text: str
    changed: bool
    replacements: list[dict[str, Any]] = Field(default_factory=list)
    confusables_seen: list[str] = Field(default_factory=list)


_ASCII_WORD = re.compile(r"^[\x21-\x7e]+$")


def _alias_pattern(alias: str) -> re.Pattern[str]:
    if _ASCII_WORD.match(alias):
        # ASCII 别名加词边界（exp 不得命中 expand）
        return re.compile(rf"(?<![\w]){re.escape(alias)}(?![\w])", re.IGNORECASE)
    return re.compile(re.escape(alias))


def parse_terms(terms_json: dict[str, Any] | None) -> list[TermRule]:
    """terms_json: {"terms": [{canonical, aliases, ...}]}（TerminologyVersion.terms）。"""
    if not terms_json:
        return []
    raw = terms_json.get("terms", []) if isinstance(terms_json, dict) else []
    rules: list[TermRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            rule = TermRule.model_validate({**item, "aliases": item.get("aliases", [])})
        except Exception:  # 单条非法不拖垮整表
            continue
        if rule.enabled:
            rules.append(rule)
    return rules


def flatten_mapping(rules: list[TermRule]) -> dict[str, str]:
    """alias → canonical 扁平映射（prompt 术语提示用）。"""
    mapping: dict[str, str] = {}
    for rule in rules:
        for alias in rule.aliases:
            mapping.setdefault(alias, rule.canonical)
    return mapping


def apply_terminology(text: str, terms_json: dict[str, Any] | None) -> TermApplyResult:
    """对已规范化文本做术语归一；逐次替换并记录 trace。"""
    rules = parse_terms(terms_json)
    result = TermApplyResult(text=text, changed=False)

    # 别名全局按长度降序：长别名优先，防止 "实验单" 先被 "实验" 截断替换
    ordered: list[tuple[TermRule, str]] = []
    for rule in rules:
        for alias in {a for a in rule.aliases if a and a != rule.canonical}:
            ordered.append((rule, alias))
    ordered.sort(key=lambda ra: len(ra[1]), reverse=True)

    for rule, alias in ordered:
        pattern = _alias_pattern(alias)
        guards = [re.compile(g) for g in rule.never_replace_when if g]
        cursor = 0
        while True:
            match = pattern.search(result.text, cursor)
            if match is None:
                break
            window = result.text[max(0, match.start() - 20): match.end() + 20]
            if any(g.search(window) for g in guards):
                cursor = match.end()
                continue
            start, end = match.span()
            result.text = result.text[:start] + rule.canonical + result.text[end:]
            result.replacements.append(
                {
                    "rule_id": rule.rule_id,
                    "source_term": alias,
                    "target_term": rule.canonical,
                    "source_span": [start, start + len(rule.canonical)],
                }
            )
            result.changed = True
            cursor = start + len(rule.canonical)

    for rule in rules:
        for confusable in rule.confusable_with:
            if confusable and confusable in text:
                result.confusables_seen.append(confusable)
    return result
