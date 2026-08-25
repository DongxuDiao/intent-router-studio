"""L0 术语归一测试（修改方案 §3.1 / §11.2 / §16.1）。"""
from __future__ import annotations

from app.query_rewrite.terminology import apply_terminology, flatten_mapping, parse_terms

TERMS = {
    "terms": [
        {"canonical": "Libra 实验", "aliases": ["libra exp", "libra_exp", "实验单"], "enabled": True},
        {"canonical": "实验", "aliases": ["exp"], "enabled": True},
        {"canonical": "灰度", "aliases": ["grayscale"], "enabled": True, "confusable_with": ["全量"]},
        {"canonical": "白名单", "aliases": ["allowlist"], "enabled": True,
         "never_replace_when": ["白名单功能"]},
        {"canonical": "已停用术语", "aliases": ["deprecated_term"], "enabled": False},
    ]
}


def test_alias_to_canonical():
    r = apply_terminology("libra exp 看下状态", TERMS)
    assert r.text == "Libra 实验 看下状态"
    assert r.changed is True
    assert r.replacements[0]["target_term"] == "Libra 实验"
    assert r.replacements[0]["rule_id"].startswith("term-")


def test_longest_alias_wins():
    # "libra exp" 必须先于 "exp" 替换，避免截断
    r = apply_terminology("查一下 libra exp", TERMS)
    assert "Libra 实验" in r.text
    assert "exp" not in r.text


def test_ascii_alias_word_boundary():
    # exp 不得命中 expand
    r = apply_terminology("expand 面板在哪", TERMS)
    assert r.text == "expand 面板在哪"
    assert r.changed is False


def test_case_insensitive_ascii():
    r = apply_terminology("Grayscale 怎么开", TERMS)
    assert r.text == "灰度 怎么开"


def test_never_replace_when_guard():
    r = apply_terminology("allowlist 与白名单功能不同", TERMS)
    # "白名单功能" 窗口内 allowlist 不替换；独立出现的 白名单 canonical 自身不替换
    assert "allowlist" in r.text
    assert r.changed is False


def test_disabled_rule_ignored():
    r = apply_terminology("deprecated_term 是什么", TERMS)
    assert r.changed is False


def test_span_trace_recorded():
    r = apply_terminology("看下 libra exp 和 exp 42", TERMS)
    spans = [(x["source_term"], x["target_term"], tuple(x["source_span"])) for x in r.replacements]
    assert ("libra exp", "Libra 实验", spans[0][2]) in spans
    assert ("exp", "实验", spans[1][2]) in spans
    # span 指向替换后文本中的目标位置
    s, e = spans[0][2]
    assert r.text[s:e] == "Libra 实验"
    s2, e2 = spans[1][2]
    assert r.text[s2:e2] == "实验"


def test_confusables_reported():
    r = apply_terminology("grayscale 还是全量", TERMS)
    assert "全量" in r.confusables_seen


def test_no_terms_passthrough():
    r = apply_terminology("原文不动", None)
    assert r.text == "原文不动" and r.changed is False and r.replacements == []


def test_flatten_mapping_and_parse_invalid_rows():
    rules = parse_terms(TERMS)
    mapping = flatten_mapping(rules)
    assert mapping["libra exp"] == "Libra 实验"
    assert "deprecated_term" not in mapping
    # 非法行被跳过
    rules2 = parse_terms({"terms": [{"canonical": ""}, "not-a-dict", {"canonical": "OK", "aliases": ["ok"]}]})
    assert [x.canonical for x in rules2] == ["OK"]
