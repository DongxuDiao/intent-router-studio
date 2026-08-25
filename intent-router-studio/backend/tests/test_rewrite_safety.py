"""Rewrite Safety Gate 测试（修改方案 §7 / §16.1）。"""
from __future__ import annotations

import pytest

from app.query_rewrite.safety import (
    EFFECT_LEVELS,
    evaluate_rewrite_safety,
    route_policy,
)
from app.query_rewrite.schemas import ProviderOutput


def _output(confidence=0.95, **kw) -> ProviderOutput:
    return ProviderOutput(standalone_query=kw.pop("standalone_query", "改写"), confidence=confidence, **kw)


CTX = "实验 123 当前流量为 10%"


def test_clean_context_resolution_allowed():
    d = evaluate_rewrite_safety(
        original="这个怎么调到 20%？",
        context=CTX,
        rewrite="如何将实验 123 的流量从 10% 调整到 20%？",
        original_route="information",
        rewrite_route="information",
        provider_output=_output(),
    )
    assert d.allow is True
    assert d.safety_decision == "allow_rewrite"
    assert d.route_conflict is False


def test_negation_drop_blocked():
    d = evaluate_rewrite_safety(
        original="先别停它",
        context="实验 123 正在运行",
        rewrite="停止实验 123",  # 否定被删除 → 命令
        original_route="write_action",
        rewrite_route="write_action",
        provider_output=_output(),
    )
    assert d.allow is False
    assert "NEGATION_CHANGED" in d.reason_codes
    assert "MODALITY_CHANGED" in d.reason_codes


def test_negation_preserved_with_rephrasing():
    d = evaluate_rewrite_safety(
        original="先别停它",
        context="实验 123 正在运行",
        rewrite="暂时不要停止实验 123",
        original_route="write_action",
        rewrite_route="write_action",
        provider_output=_output(),
    )
    # 原 1 处否定（别），改写 1 处（不，来自"不要"）→ 一致
    assert "NEGATION_CHANGED" not in d.reason_codes


def test_invented_number_blocked():
    d = evaluate_rewrite_safety(
        original="这个怎么调到 20%？",
        context=CTX,
        rewrite="如何将实验 456 的流量调整到 20%？",  # 456 凭空出现
        original_route="information",
        rewrite_route="information",
        provider_output=_output(),
    )
    assert d.allow is False
    assert "OBJECT_INVENTED" in d.reason_codes


def test_term_target_counts_as_traceable():
    d = evaluate_rewrite_safety(
        original="libra exp 怎么开灰度",
        context=None,
        rewrite="Libra 实验怎么开灰度",
        original_route="information",
        rewrite_route="information",
        provider_output=_output(),
        term_targets=["Libra 实验"],  # L0 引入的标准术语
    )
    assert "OBJECT_INVENTED" not in d.reason_codes


def test_new_high_risk_action_blocked():
    d = evaluate_rewrite_safety(
        original="看下实验 123 的状态",
        context=None,
        rewrite="查看实验 123 的状态并删除实验 456",
        original_route="read_only",
        rewrite_route="read_only",
        provider_output=_output(),
    )
    assert d.allow is False
    assert "ACTION_INTENSIFIED" in d.reason_codes


def test_verb_traceable_via_first_char():
    # 原文"调到"含"调"，改写用"调整"不算新增动作
    d = evaluate_rewrite_safety(
        original="这个怎么调到 20%？",
        context=CTX,
        rewrite="如何将实验 123 的流量调整到 20%？",
        original_route="information",
        rewrite_route="information",
        provider_output=_output(),
    )
    assert "ACTION_INTENSIFIED" not in d.reason_codes


def test_escalation_always_blocked():
    d = evaluate_rewrite_safety(
        original="怎么停止实验 123？",
        context=None,
        rewrite="如何停止实验 123？",
        original_route="information",
        rewrite_route="write_action",  # 影子分类判为写
        provider_output=_output(),
        # 即使项目关闭一致性要求，升级也是安全不变量
        require_route_consistency=False,
    )
    assert d.allow is False
    assert d.escalation is True
    assert "ROUTE_CONFLICT" in d.reason_codes


def test_write_downgrade_blocked():
    d = evaluate_rewrite_safety(
        original="帮我停止实验 123",
        context=None,
        rewrite="如何停止实验 123？",
        original_route="write_action",
        rewrite_route="information",
        provider_output=_output(),
    )
    assert d.allow is False
    assert d.downgrade is True
    assert d.route_policy.formal_route == "write_action"  # §7.4 正式路由保持原文


def test_route_consistency_can_be_relaxed_except_escalation():
    d = evaluate_rewrite_safety(
        original="查下实验 123",
        context=None,
        rewrite="查询实验 123",
        original_route="information",
        rewrite_route="read_only",
        provider_output=_output(),
        require_route_consistency=False,
    )
    assert d.allow is True  # 非升级冲突且项目允许不一致


def test_low_confidence_blocked():
    d = evaluate_rewrite_safety(
        original="这个怎么调到 20%？",
        context=CTX,
        rewrite="如何将实验 123 的流量从 10% 调整到 20%？",
        original_route="information",
        rewrite_route="information",
        provider_output=_output(confidence=0.5),
        confidence_threshold=0.8,
    )
    assert d.allow is False
    assert "LOW_CONFIDENCE" in d.reason_codes


def test_provider_reported_intent_loss_blocked():
    d = evaluate_rewrite_safety(
        original="把那个关了",
        context=None,
        rewrite="把那个关了",
        original_route="unclear",
        rewrite_route="unclear",
        provider_output=_output(preserved_intent=False),
    )
    assert "UNSUPPORTED_ASSUMPTION" in d.reason_codes


def test_unchanged_text_skips_semantic_checks():
    d = evaluate_rewrite_safety(
        original="今天天气怎么样",
        context=None,
        rewrite="今天天气怎么样",
        original_route="oos",
        rewrite_route="oos",
        provider_output=_output(),
        changed=False,
    )
    assert any(c.name == "semantic_checks_skipped" for c in d.checks)
    assert d.allow is True


def test_route_policy_matrix():
    assert route_policy("information", "information").downstream_rewrite_allowed
    assert not route_policy("information", "read_only").downstream_rewrite_allowed
    assert not route_policy("read_only", "information").downstream_rewrite_allowed
    esc = route_policy("read_only", "write_action")
    assert esc.escalation and not esc.downstream_rewrite_allowed
    dwn = route_policy("write_action", "information")
    assert dwn.downgrade and dwn.formal_route == "write_action"
    assert not route_policy("unclear", "information").downstream_rewrite_allowed
    assert not route_policy("oos", "information").downstream_rewrite_allowed
    assert not route_policy("information", "oos").downstream_rewrite_allowed
    assert EFFECT_LEVELS["write_action"] > EFFECT_LEVELS["read_only"] > EFFECT_LEVELS["information"]


@pytest.mark.parametrize(
    "orig,rw,expect_allow",
    [
        ("information", "information", True),
        ("read_only", "read_only", True),
        ("unclear", "unclear", True),
        ("oos", "oos", True),
        ("write_action", "write_action", True),
        ("information", "write_action", False),
        ("read_only", "write_action", False),
        ("unclear", "write_action", False),
        ("oos", "write_action", False),
        ("write_action", "information", False),
    ],
)
def test_matrix_effect_levels(orig, rw, expect_allow):
    p = route_policy(orig, rw)
    assert p.downstream_rewrite_allowed is expect_allow
    assert p.formal_route == orig  # §7.4：正式路由恒为原文


# ---- V2 §3.2 写动作对象溯源（失败关闭） ----


def test_write_object_completion_without_context_blocked():
    # `删除它` + 无 Context：指代无从解析，改写补充任何对象都算凭空捏造
    d = evaluate_rewrite_safety(
        original="删除它",
        context=None,
        rewrite="删除实验 123",
        original_route="write_action",
        rewrite_route="write_action",
        provider_output=_output(),
    )
    assert d.allow is False
    assert "OBJECT_INVENTED" in d.reason_codes
    assert d.route_policy.formal_route == "write_action"  # 路由仍由原文决定


def test_write_object_completion_from_context_allowed():
    # Context 明确出现"实验 123"→ 允许补全
    d = evaluate_rewrite_safety(
        original="删除它",
        context="实验 123 已失效",
        rewrite="删除实验 123",
        original_route="write_action",
        rewrite_route="write_action",
        provider_output=_output(),
    )
    assert d.allow is True
    assert "OBJECT_INVENTED" not in d.reason_codes


def test_common_chinese_noun_invention_blocked():
    # 普通中文名词（非数字/英文 ID）同样不得绕过溯源：
    # `删除它` → `删除飞书实验`，上下文里没有"飞书"→ OBJECT_INVENTED
    d = evaluate_rewrite_safety(
        original="删除它",
        context="实验 123 已失效",
        rewrite="删除飞书实验",
        original_route="write_action",
        rewrite_route="write_action",
        provider_output=_output(),
    )
    assert d.allow is False
    assert "OBJECT_INVENTED" in d.reason_codes
    check = next(c for c in d.checks if c.name == "objects_traceable")
    assert check.passed is False and "飞书实验" in check.detail


def test_negation_from_context_counts_in_range():
    # 否定检查基于 original+context：改写可以带上上下文中的否定，不能凭空多出
    d_ok = evaluate_rewrite_safety(
        original="先别停它",
        context="用户强调不要现在停止实验 123",
        rewrite="暂时不要停止实验 123",
        original_route="write_action",
        rewrite_route="write_action",
        provider_output=_output(),
    )
    assert "NEGATION_CHANGED" not in d_ok.reason_codes
    # 改写凭空新增第三处否定 → 拦截
    d_extra = evaluate_rewrite_safety(
        original="先别停它",
        context="实验 123 正在运行",
        rewrite="不确认的话，不要不能停止实验 123",
        original_route="write_action",
        rewrite_route="write_action",
        provider_output=_output(),
    )
    assert "NEGATION_CHANGED" in d_extra.reason_codes


def test_read_to_write_escalation_never_applied():
    # information/read_only 被改写成 write_action：无论配置如何都禁止
    for orig_route in ("information", "read_only"):
        d = evaluate_rewrite_safety(
            original="看下实验 123 的状态",
            context=None,
            rewrite="帮我删除实验 123",
            original_route=orig_route,
            rewrite_route="write_action",
            provider_output=_output(),
            require_route_consistency=False,
        )
        assert d.allow is False
        assert d.escalation is True
        assert d.route_policy.formal_route == orig_route


def test_pronoun_resolution_uses_term_targets():
    # L0 术语归一引入的标准术语对象同样可溯源
    d = evaluate_rewrite_safety(
        original="libra exp 删掉它",
        context=None,
        rewrite="删除 Libra 实验",
        original_route="write_action",
        rewrite_route="write_action",
        provider_output=_output(),
        term_targets=["Libra 实验"],
    )
    assert "OBJECT_INVENTED" not in d.reason_codes
