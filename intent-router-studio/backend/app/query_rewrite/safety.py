"""Rewrite Safety Gate（修改方案 §7 / V2 §3.2）。

检查项（§7.1 + V2 补充）：Schema 合法（入口已校验）、非空与长度、否定一致、
语气不强化、实体可追溯、动作对象溯源（V2 §3.2：改写新增的普通中文名词同样
必须能追溯到 original+context+术语表，无法溯源即失败关闭）、无新增高风险动作、
置信度阈值、路由一致性（§7.2/§7.3 效果等级与矩阵）。

首版正式决策（§7.4）：final_route 恒等于 original_route；
本门只决定 downstream_query 能否采用改写（且仅在 safe_apply 模式被消费）。
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.query_rewrite.schemas import MAX_STANDALONE_CHARS, ProviderOutput

# §7.2 效果等级：none(0) < read_only(1) < external_write_candidate(2)
EFFECT_LEVELS: dict[str, int] = {
    "write_action": 2,
    "read_only": 1,
    "information": 0,
    "unclear": 0,
    "oos": 0,
}

# 中文否定（多字优先，避免"不要"被计两次）
_NEGATION_RE = re.compile(r"不要|不能|无法|没有|别|不|勿|莫|没|无|非|禁止")
# 疑问 / 咨询语气标记
_QUESTION_RE = re.compile(r"怎么|怎样|如何|什么|为什么|哪里|哪个|能否|能不能|可不可以|可以吗|吗|？|\?|想了解|想知道|请问|告诉我|查一下|查下|看看|看下")
# 命令 / 强化前缀；动词起头的裸命令（"停止实验 123"）同样算 imperative
_IMPERATIVE_RE = re.compile(r"帮我|给我|请把|把.{0,12}(改成|改为|调整|删除|停|关)|直接|立刻|立即|马上")
# 高风险（写域）动作词；追溯到原文时允许首字命中（停 → 先别停它）
_HIGH_RISK_VERBS = (
    "删除", "删掉", "停止", "停掉", "关闭", "关掉", "下线", "上线", "发布", "回滚",
    "撤回", "撤销", "修改", "改成", "改为", "更新", "创建", "新建", "添加", "移除",
    "清空", "重置", "执行", "审批", "恢复", "终止", "禁用", "启用",
)
# 实体 token：数字 / 百分比 / 字母数字 ID / 中文数字串
_ENTITY_PATTERNS = (
    re.compile(r"\d+(?:\.\d+)?%?"),
    re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}"),
    re.compile(r"[一二三四五六七八九十百千万]{2,}"),
)

# ---- V2 §3.2 动作对象溯源 ----------------------------------------------------
# 威胁：改写给写意图补充一个原文/上下文里不存在的对象（"删除它"→"删除飞书实验"），
# 旧实体检查只覆盖数字/英文 ID/中文数字串，普通中文名词完全绕过。
# 策略（失败关闭）：把改写文本剥掉功能词与动词后，剩余 ≥2 字的中文内容块一律
# 视为"对象/实体"，必须能在 original+context+术语表（corpus）中找到，否则 OBJECT_INVENTED。
#
# 允许无需溯源的词汇 = 功能词 + 常见读域动词 + 全部高风险动词（动作语义另由检查 6 约束）。
_FUNCTION_TOKENS = (
    # 疑问 / 咨询语气
    "请问", "如何", "怎么", "怎样", "怎么样", "为什么", "什么", "哪个", "哪些", "哪里",
    "是否", "能否", "能不能", "可不可以", "可以吗", "想了解", "想知道", "告诉我",
    "查一下", "查下", "看看", "看下", "一下", "吗", "呢", "吧", "啊",
    # 否定 / 限制（与 _NEGATION_RE 对齐 + 时间限定）
    "不要", "不能", "无法", "没有", "禁止", "暂时", "现在", "当前", "今天", "先",
    # 代词 / 指示
    "它", "这个", "那个", "这些", "那些", "该", "此", "我", "你", "我们", "你们", "他们", "它们",
    # 介词 / 连词 / 助词 / 语气
    "以及", "然后", "并且", "或者", "帮我", "给我", "麻烦", "请",
    "的", "了", "是", "在", "把", "被", "将", "给", "对", "和", "与", "或", "及", "并",
    "从", "到", "就", "都", "也", "还", "再", "又", "才", "正", "正在",
    # 常见低风险（读域/归一）动词
    "查看", "查询", "检索", "搜索", "显示", "展示", "列出", "获取", "打开", "开启",
    "处理", "调整", "看", "找", "搜",
)
# 动词首字集合：剥离动词变体头（"停掉/关停/删了"等未收录组合）
_VERB_HEAD_CHARS = {v[0] for v in _HIGH_RISK_VERBS} | {"查", "看", "找", "搜", "显", "列", "获"}
# 剥离词表按长度降序，保证"不要"先于"不"、"删除"先于"删"
_STRIP_TOKENS = tuple(sorted({*_FUNCTION_TOKENS, *_HIGH_RISK_VERBS}, key=len, reverse=True))
# 中文内容块（≥2 字连续汉字）
_CJK_RUN_RE = re.compile(r"[一-鿿]{2,}")


class SafetyCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class RoutePolicy(BaseModel):
    """§7.3 一致性矩阵的单行决策。formal_route 首版恒为原文路由（§7.4）。"""

    downstream_rewrite_allowed: bool = False
    formal_route: str
    conflict: bool
    escalation: bool = False  # 非写 → 写（false_write_escalation）
    downgrade: bool = False   # 写 → 非写（write_downgrade）
    note: str = ""


class SafetyDecision(BaseModel):
    allow: bool
    safety_decision: str  # allow_rewrite | blocked（服务层另有 fallback_original）
    reason_codes: list[str] = Field(default_factory=list)
    checks: list[SafetyCheck] = Field(default_factory=list)
    route_conflict: bool = False
    escalation: bool = False
    downgrade: bool = False
    route_policy: RoutePolicy | None = None


def route_policy(original_route: str, rewrite_route: str) -> RoutePolicy:
    """§7.3 一致性矩阵。downstream_rewrite_allowed 仅表示"路由维度不阻止"。"""
    base: dict[str, object] = {"formal_route": original_route}
    if original_route == rewrite_route:
        return RoutePolicy(**base, conflict=False, downstream_rewrite_allowed=True, note="路由一致")
    escalation = rewrite_route == "write_action" and original_route != "write_action"
    downgrade = original_route == "write_action" and rewrite_route != "write_action"
    if escalation:
        return RoutePolicy(**base, conflict=True, escalation=True, note="非写意图被改写为写，禁止应用")
    if downgrade:
        return RoutePolicy(**base, conflict=True, downgrade=True, note="写意图被淡化，保留原文并继续确认门")
    if original_route == "unclear":
        return RoutePolicy(**base, conflict=True, note="原文 unclear，默认原文路由进入澄清")
    if original_route == "oos" or rewrite_route == "oos":
        return RoutePolicy(**base, conflict=True, note="oos 边界冲突，保留原文")
    return RoutePolicy(**base, conflict=True, downstream_rewrite_allowed=False, note="information/read_only 之间漂移，保留原文")


def _count_negation(text: str) -> int:
    return len(_NEGATION_RE.findall(text or ""))


def _has_question(text: str) -> bool:
    return bool(_QUESTION_RE.search(text or ""))


def _has_imperative(text: str) -> bool:
    text = (text or "").strip()
    if _IMPERATIVE_RE.search(text):
        return True
    return any(text.startswith(verb) for verb in _HIGH_RISK_VERBS)


def _extract_entities(text: str) -> list[str]:
    found: list[str] = []
    for pattern in _ENTITY_PATTERNS:
        found.extend(m.group(0) for m in pattern.finditer(text or ""))
    return found


def _untraceable_entities(rewrite: str, corpus: str) -> list[str]:
    missing = []
    for token in _extract_entities(rewrite):
        if token not in corpus:
            missing.append(token)
    return missing


def _content_chunks(rewrite: str) -> list[str]:
    """提取需要溯源的中文内容块：剥离功能词/动词后的 ≥2 字连续汉字段。

    例："如何将实验 123 的流量调整到 20%？" → ["实验", "流量"]；
    "删除飞书实验" → ["飞书实验"]（动词"删除"被剥离，对象保留待查）。
    """
    stripped = rewrite or ""
    for token in _STRIP_TOKENS:
        stripped = stripped.replace(token, "\x00")
    chunks: list[str] = []
    for run in _CJK_RUN_RE.findall(stripped):
        # 残留的动词变体头（"停掉/关停"）：从头部逐字剥离动词首字与常见补语
        start = 0
        complements = "掉了过住起着"
        while start < len(run) and (run[start] in _VERB_HEAD_CHARS or run[start] in complements):
            start += 1
        piece = run[start:]
        if len(piece) >= 2:
            chunks.append(piece)
    return chunks


def _untraceable_content(rewrite: str, corpus: str) -> list[str]:
    """改写中出现、但 corpus（original+context+术语表）里找不到的中文内容块。"""
    return [chunk for chunk in _content_chunks(rewrite) if chunk not in corpus]


def _new_high_risk_actions(rewrite: str, corpus: str) -> list[str]:
    new_verbs = []
    for verb in _HIGH_RISK_VERBS:
        if verb in rewrite and verb not in corpus and verb[0] not in corpus:
            new_verbs.append(verb)
    return new_verbs


def evaluate_rewrite_safety(
    original: str,
    context: str | None,
    rewrite: str,
    original_route: str,
    rewrite_route: str,
    provider_output: ProviderOutput | None = None,
    confidence_threshold: float = 0.8,
    require_route_consistency: bool = True,
    term_targets: list[str] | None = None,
    changed: bool = True,
) -> SafetyDecision:
    """执行 §7.1 全部检查。返回 allow=改写可用于 downstream_query 的最终判定。"""
    checks: list[SafetyCheck] = []
    reason_codes: list[str] = []
    corpus = f"{original}\n{context or ''}"
    if term_targets:
        corpus += "\n" + "\n".join(term_targets)

    # 1/2. 非空与长度（Schema 校验兜底）
    ok_nonempty = bool(rewrite and rewrite.strip()) and len(rewrite) <= MAX_STANDALONE_CHARS
    checks.append(SafetyCheck(name="nonempty_and_length", passed=ok_nonempty, detail=f"len={len(rewrite or '')}"))
    if not ok_nonempty:
        reason_codes.append("INVALID_JSON")

    # 语义检查仅在文本确实变化时进行
    if changed and ok_nonempty:
        # 3. 否定一致（V2 §3.2：以 original+context 为基准区间——
        # 改写可以补出上下文中明确存在的否定，但不能少于原文或凭空多出）
        neg_orig, neg_rw = _count_negation(original), _count_negation(rewrite)
        neg_ceiling = _count_negation(corpus)
        ok_neg = neg_orig <= neg_rw <= neg_ceiling
        checks.append(SafetyCheck(name="negation_preserved", passed=ok_neg, detail=f"原文否定 {neg_orig} 处 / 改写 {neg_rw} 处 / 上限（含上下文）{neg_ceiling} 处"))
        if not ok_neg:
            reason_codes.append("NEGATION_CHANGED")

        # 4. 语气不强化（V2 §3.2：原文侧基准同样包含 context——
        # 上下文若已是命令/疑问，改写维持该语气不算强化）
        orig_had_question = _has_question(original) or _has_question(context or "")
        orig_had_imperative = _has_imperative(original) or _has_imperative(context or "")
        intensified = (orig_had_question and not _has_question(rewrite) and _has_imperative(rewrite)) or (
            not orig_had_imperative and _has_imperative(rewrite)
        )
        checks.append(SafetyCheck(name="modality_preserved", passed=not intensified, detail="疑问/命令语气未被强化" if not intensified else "疑问被改写为命令或新增命令前缀"))
        if intensified:
            reason_codes.append("MODALITY_CHANGED")
            reason_codes.append("ACTION_INTENSIFIED")

        # 5. 实体可追溯 + 动作对象溯源（V2 §3.2 失败关闭：普通中文名词同样必须可追溯）
        missing_entities = _untraceable_entities(rewrite, corpus)
        checks.append(SafetyCheck(name="entities_traceable", passed=not missing_entities, detail=f"不可追溯: {missing_entities[:5]}" if missing_entities else "ID/数值/对象均可追溯"))
        if missing_entities:
            reason_codes.append("OBJECT_INVENTED")
        missing_objects = _untraceable_content(rewrite, corpus)
        checks.append(SafetyCheck(name="objects_traceable", passed=not missing_objects, detail=f"无法溯源的对象: {missing_objects[:5]}" if missing_objects else "动作对象均可溯源（含普通中文名词）"))
        if missing_objects:
            reason_codes.append("OBJECT_INVENTED")

        # 6. 无新增高风险动作
        new_verbs = _new_high_risk_actions(rewrite, corpus)
        checks.append(SafetyCheck(name="no_new_high_risk_action", passed=not new_verbs, detail=f"新增动作: {new_verbs[:5]}" if new_verbs else "无新增高风险动作"))
        if new_verbs:
            reason_codes.append("ACTION_INTENSIFIED")
    else:
        checks.append(SafetyCheck(name="semantic_checks_skipped", passed=True, detail="文本未变化，语义检查跳过"))

    # 7. 置信度（confidence 表示忠实度，不构成授权 —— §2.3）
    confidence = provider_output.confidence if provider_output is not None else 1.0
    ok_conf = confidence >= confidence_threshold
    checks.append(SafetyCheck(name="confidence_threshold", passed=ok_conf, detail=f"{confidence:.2f} >= {confidence_threshold}"))
    if not ok_conf:
        reason_codes.append("LOW_CONFIDENCE")
    if provider_output is not None and not provider_output.preserved_intent:
        checks.append(SafetyCheck(name="provider_preserved_intent", passed=False, detail="模型自报意图未保留"))
        reason_codes.append("UNSUPPORTED_ASSUMPTION")
    else:
        checks.append(SafetyCheck(name="provider_preserved_intent", passed=True, detail="模型自报意图保留"))

    # 8. 路由一致性（§7.2：效果等级升级一律禁止；§7.3 矩阵）
    policy = route_policy(original_route, rewrite_route)
    route_ok = policy.downstream_rewrite_allowed or not require_route_consistency
    # 升级是安全不变量：无论配置如何都阻止（escalation 已在 policy 中不允许 downstream）
    route_ok = route_ok and not policy.escalation
    checks.append(
        SafetyCheck(
            name="route_consistency",
            passed=policy.downstream_rewrite_allowed,
            detail=policy.note,
        )
    )
    if not policy.downstream_rewrite_allowed:
        reason_codes.append("ROUTE_CONFLICT")

    allow = all(c.passed for c in checks if c.name != "route_consistency") and route_ok
    # 去重并保序
    seen: set[str] = set()
    ordered_codes = [c for c in reason_codes if not (c in seen or seen.add(c))]
    return SafetyDecision(
        allow=allow,
        safety_decision="allow_rewrite" if allow else "blocked",
        reason_codes=ordered_codes,
        checks=checks,
        route_conflict=policy.conflict,
        escalation=policy.escalation,
        downgrade=policy.downgrade,
        route_policy=policy,
    )
