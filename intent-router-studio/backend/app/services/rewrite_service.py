"""Query 改写编排服务（修改方案 §2.1 / §9.3 / §12）。

链路：L0 规范化+术语归一 → （生成模式）provider 改写 → 改写文本影子分类 →
Rewrite Safety Gate → downstream_query 决策。

不变量：
- final_route 恒等于原文路由（§7.4），改写永不修改正式五分类结果
- Provider 超时/不可用/非法 JSON → 记录 reason_code、downstream 用原文、正常返回（§9.4）
- 缓存键包含全部影响改写结果的版本（§12.1）
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from typing import Any

from sqlalchemy.orm import Session

from app import ids
from app.config import get_settings
from app.errors import ApiError, NotFoundError
from app.models import Project, RewriteConfigVersion, RewriteFeedback, TerminologyVersion
from app.query_rewrite.cache import RewriteCache, build_cache_key
from app.query_rewrite.client import RewriteClient
from app.query_rewrite.prompt import PROMPT_VERSION
from app.query_rewrite.provider import ProviderBusy, ProviderTimeout, ProviderUnavailable
from app.query_rewrite.safety import SafetyDecision, evaluate_rewrite_safety
from app.query_rewrite.schemas import (
    ProviderOutput,
    RewriteModelInfo,
    RewriteParseError,
    RewriteResult,
)
from app.query_rewrite.terminology import TermApplyResult, apply_terminology, flatten_mapping, parse_terms
from app.router_core.normalization import normalize_text, text_hash
from app.utils.hashing import sha256_bytes

logger = logging.getLogger("app.rewrite")

# §4 默认配置
REWRITE_CONFIG_DEFAULTS: dict[str, Any] = {
    "mode": "shadow",
    "timeout_ms": 90000,
    "min_rewrite_confidence": 0.8,
    "require_route_consistency": True,
    "fallback": "original",
    "store_raw_text": False,
    # 外部模型 V1 §6.2：默认本地 Qwen，现有项目行为不变；
    # 该字段随配置版本化（可回滚），密钥/URL/模型参数仍在连接表中
    "provider_connection_id": "builtin:local_qwen",
}

# V2 §4.3 方案A：生成模型参数（provider/model/device/生成上限）只由部署环境管理
# ——即 rewriter 服务的 REWRITE_* 环境变量。项目级配置仅保留策略字段；
# 这些键出现在项目配置中会被拒绝，避免"保存后不生效"的假配置。
# 外部模型 V1 §6.2 追加 api_key：密钥只能进连接表（加密），永不进项目配置。
DEPLOYMENT_OWNED_KEYS = ("provider", "model_id", "device", "max_new_tokens", "max_context_chars", "base_url", "api_key")

VALID_MODES = ("off", "normalize_only", "shadow", "safe_apply")

# 进程内共享：HTTP 客户端（熔断）与改写缓存
_client: RewriteClient | None = None
CACHE = RewriteCache()

# §17.1 可观测性：进程内计数器（无外部依赖）
METRICS: dict[str, Any] = {
    "requests_total": 0,
    "success_total": 0,
    "fallback_total": {},        # reason -> count
    "route_conflict_total": {},  # "from->to" -> count（效果层）
    "intent_drift_total": {},    # "from->to" -> count（业务意图层，Review 修复 §8.1）
    "safety_reject_total": {},   # reason -> count
    "cache_hit_total": 0,
    "latency_samples": deque(maxlen=500),
}


def _metrics_inc(key: str, label: str | None = None) -> None:
    if label is None:
        METRICS[key] += 1
    else:
        bucket = METRICS[key]
        bucket[label] = bucket.get(label, 0) + 1


def get_client() -> RewriteClient:
    global _client
    if _client is None:
        settings = get_settings()
        CACHE.__init__(
            capacity=settings.rewrite_cache_capacity, ttl_s=settings.rewrite_cache_ttl_hours * 3600
        )
        _client = RewriteClient(
            base_url=settings.rewriter_url,
            timeout_ms=settings.rewrite_timeout_ms,
            failure_threshold=settings.rewrite_failure_threshold,
            open_seconds=settings.rewrite_breaker_open_seconds,
        )
        # 外部模型 V1 §8.2：连接更新 / 删除 / 测试成功 → 清除该连接熔断状态
        from app.services import provider_connection_service

        def _on_connection_change(event: str, row) -> None:
            client = _client
            if client is None or row is None:
                return
            if event in ("changed", "test_ok"):
                client.clear_connection_state(getattr(row, "id", None))

        provider_connection_service.register_listener(_on_connection_change)
    return _client


def reset_client() -> None:
    """测试辅助：清空进程内单例与指标。"""
    global _client
    _client = None
    CACHE.clear()
    for key in ("requests_total", "success_total", "cache_hit_total"):
        METRICS[key] = 0
    for key in ("fallback_total", "route_conflict_total", "intent_drift_total", "safety_reject_total"):
        METRICS[key] = {}
    METRICS["latency_samples"].clear()


def metrics_snapshot() -> dict[str, Any]:
    samples = sorted(METRICS["latency_samples"])
    if samples:
        p50 = samples[len(samples) // 2]
        p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    else:
        p50 = p95 = None
    return {
        "requests_total": METRICS["requests_total"],
        "success_total": METRICS["success_total"],
        "fallback_total": dict(METRICS["fallback_total"]),
        "route_conflict_total": dict(METRICS["route_conflict_total"]),
        "intent_drift_total": dict(METRICS["intent_drift_total"]),
        "safety_reject_total": dict(METRICS["safety_reject_total"]),
        "cache_hit_total": METRICS["cache_hit_total"],
        "rewrite_latency_ms": {"p50": p50, "p95": p95, "n": len(samples)},
        "cache_size": len(CACHE),
    }


# ---------------------------------------------------------------- 配置与术语

def validate_rewrite_config(config: dict[str, Any]) -> list[str]:
    """结构校验（§10.3 validate）；允许部分配置（缺失项按默认值校验）。

    V2 §4.3 方案A：部署所有的生成模型键（provider/model_id 等）直接拒绝。
    返回问题列表，空列表 = 合法。
    """
    merged = {**REWRITE_CONFIG_DEFAULTS, **(config or {})}
    problems: list[str] = []
    deployment_owned = sorted(set(DEPLOYMENT_OWNED_KEYS) & set(config or {}))
    if deployment_owned:
        problems.append(
            f"以下字段由部署配置管理（rewriter REWRITE_* 环境变量），不能在项目级修改: {deployment_owned}"
        )
    mode = merged.get("mode")
    if mode not in VALID_MODES:
        problems.append(f"mode 必须是 {'/'.join(VALID_MODES)} 之一")
    conf = merged.get("min_rewrite_confidence")
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not 0.0 <= float(conf) <= 1.0:
        problems.append("min_rewrite_confidence 必须在 [0,1]")
    tmo = merged.get("timeout_ms")
    # 上限 120s：产品默认 5s 面向 GPU；纯 CPU 部署一条 JSON 改写可达 60-90s
    if not isinstance(tmo, int) or isinstance(tmo, bool) or not 200 <= tmo <= 120_000:
        problems.append("timeout_ms 必须在 [200, 120000]")
    if merged.get("fallback", "original") != "original":
        problems.append("fallback 首版仅支持 original")
    if not isinstance(merged.get("require_route_consistency", True), bool):
        problems.append("require_route_consistency 必须是布尔")
    if not isinstance(merged.get("store_raw_text", False), bool):
        problems.append("store_raw_text 必须是布尔")
    connection_id = merged.get("provider_connection_id")
    if connection_id is not None and (not isinstance(connection_id, str) or not connection_id.strip()):
        problems.append("provider_connection_id 必须是非空字符串（builtin:local_qwen 或连接 ID）")
    return problems


def _active_terminology(db: Session, project_id: str) -> tuple[str, dict[str, Any]]:
    row = (
        db.query(TerminologyVersion)
        .filter(TerminologyVersion.project_id == project_id)
        .order_by(TerminologyVersion.version.desc())
        .first()
    )
    if row is None:
        return "none", {}
    return row.id, row.terms or {}


def get_project_rewrite_config(db: Session, project_id: str) -> dict[str, Any]:
    """返回生效配置（合并默认值）与版本标识（供缓存键 / trace 使用）。

    V2 §4.3 方案A：读取时剥离历史版本中残留的部署字段，项目配置只剩策略项。
    """
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project", project_id)
    config = dict(REWRITE_CONFIG_DEFAULTS)
    config_version_id = "defaults"
    if project.active_rewrite_config_id:
        version = db.get(RewriteConfigVersion, project.active_rewrite_config_id)
        if version is not None:
            config.update(version.config or {})
            config_version_id = version.id
    config = {k: v for k, v in config.items() if k not in DEPLOYMENT_OWNED_KEYS}
    config.setdefault("provider_connection_id", "builtin:local_qwen")  # 旧配置读取时补默认（V1 §16.1）
    from app.services import provider_connection_service

    provider = provider_connection_service.connection_snapshot(db, config["provider_connection_id"])
    terminology_version_id, terms = _active_terminology(db, project_id)
    return {
        "config": config,
        "config_version_id": config_version_id,
        "terminology_version_id": terminology_version_id,
        "terms": terms,
        "provider": provider,
    }


def deployment_info() -> dict[str, Any]:
    """V2 §4.3 方案A：生成模型参数只读展示，来源为 rewriter 部署的健康信息。"""
    health = get_client().health()
    rw = health.get("rewriter") or {}
    return {
        "available": bool(rw.get("ok")),
        "provider": rw.get("provider"),
        "model_id": rw.get("model_id"),
        "device": rw.get("device"),
        "max_new_tokens": rw.get("max_new_tokens"),
        "prompt_version": rw.get("prompt_version"),
        "note": "由部署环境管理（rewriter REWRITE_* 环境变量），项目级只读",
    }


def put_rewrite_config(db: Session, project_id: str, config: dict[str, Any]) -> RewriteConfigVersion:
    """版本化保存并激活（§10.3：新建版本，不原地覆盖）。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project", project_id)
    problems = validate_rewrite_config(config)
    if problems:
        raise ApiError("VALIDATION_ERROR", "改写配置不合法", 422, {"problems": problems})
    # 外部模型 V1 §6.2：连接引用合法且可用才允许保存（密钥/URL 仍禁止进入配置）
    from app.services import provider_connection_service

    provider_connection_service.validate_connection_for_config(
        db, (config or {}).get("provider_connection_id") or "builtin:local_qwen"
    )
    last = (
        db.query(RewriteConfigVersion)
        .filter(RewriteConfigVersion.project_id == project_id)
        .order_by(RewriteConfigVersion.version.desc())
        .first()
    )
    row = RewriteConfigVersion(
        id=ids.prefixed(ids.REWRITE_CONFIG),
        project_id=project_id,
        version=(last.version + 1) if last else 1,
        config=config,
        hash=sha256_bytes(json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")),
        status="ACTIVE",
    )
    if project.active_rewrite_config_id:
        previous = db.get(RewriteConfigVersion, project.active_rewrite_config_id)
        if previous is not None:
            previous.status = "ARCHIVED"
    db.add(row)
    project.active_rewrite_config_id = row.id
    db.commit()
    db.refresh(row)
    CACHE.clear_project(project_id)
    return row


def list_rewrite_configs(db: Session, project_id: str) -> list[RewriteConfigVersion]:
    return (
        db.query(RewriteConfigVersion)
        .filter(RewriteConfigVersion.project_id == project_id)
        .order_by(RewriteConfigVersion.version.desc())
        .all()
    )


def put_terminology(db: Session, project_id: str, terms: dict[str, Any]) -> TerminologyVersion:
    """术语表版本化保存（§11.2）。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project", project_id)
    raw = terms.get("terms", []) if isinstance(terms, dict) else []
    problems: list[str] = []
    if not isinstance(raw, list):
        problems.append("terms 必须是列表")
    else:
        for i, item in enumerate(raw[:5]):
            if not isinstance(item, dict) or not item.get("canonical"):
                problems.append(f"terms[{i}] 缺少 canonical 字段")
    if problems:
        raise ApiError("VALIDATION_ERROR", "术语表不合法", 422, {"problems": problems})
    last = (
        db.query(TerminologyVersion)
        .filter(TerminologyVersion.project_id == project_id)
        .order_by(TerminologyVersion.version.desc())
        .first()
    )
    row = TerminologyVersion(
        id=ids.prefixed(ids.TERMINOLOGY),
        project_id=project_id,
        version=(last.version + 1) if last else 1,
        terms=terms,
        hash=sha256_bytes(
            json.dumps(terms, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    CACHE.clear_project(project_id)
    return row


# ---------------------------------------------------------------- 核心编排

_EMPTY_TERM = TermApplyResult(text="", changed=False, replacements=[], confusables_seen=[])
_CONTEXT_ENTITY_RE = re.compile(r"(实验|项目|报告|任务)\s*([A-Za-z0-9][A-Za-z0-9_.:-]{0,63})")


def _fast_context_rewrite(query: str, context: str | None) -> ProviderOutput | None:
    """确定性解析“这个实验/该项目”等明确指代；复杂省略仍交给模型。"""
    if not context:
        return None
    entities: dict[str, set[str]] = {}
    for kind, identifier in _CONTEXT_ENTITY_RE.findall(context):
        entities.setdefault(kind, set()).add(f"{kind} {identifier}")

    rewritten = query
    for kind, values in entities.items():
        if len(values) != 1:
            continue
        entity = next(iter(values))
        for reference in (f"这个{kind}", f"那个{kind}", f"该{kind}", f"这份{kind}"):
            rewritten = rewritten.replace(reference, entity + " ")
    rewritten = re.sub(r"\s+", " ", rewritten).strip()
    if rewritten == query:
        return None
    return ProviderOutput(
        standalone_query=rewritten,
        rewrite_type="context_resolution",
        confidence=0.99,
        reason_codes=["RESOLVED_PRONOUN"],
    )


def _l0_result(text: str, normalized: str, term_result: TermApplyResult) -> RewriteResult:
    """L0-only RewriteResult（off / normalize_only 路径，无生成模型参与）。"""
    changed = term_result.changed or normalized != text
    if term_result.changed:
        rtype, codes = "term_normalization", ["NORMALIZED_TERM"]
    elif normalized != text:
        rtype, codes = "normalization", ["NO_REWRITE_NEEDED"]
    else:
        rtype, codes = "none", ["NO_REWRITE_NEEDED"]
    return RewriteResult(
        original_query=text,
        normalized_query=normalized,
        standalone_query=term_result.text or normalized,
        rewrite_type=rtype,
        changed=changed,
        should_use=changed,
        confidence=1.0,
        preserved_intent=True,
        reason_codes=codes,
        model=RewriteModelInfo(provider="terminology", model_id="L0", prompt_version=PROMPT_VERSION),
        term_replacements=term_result.replacements,
    )


def _fallback_payload(
    text: str, normalized: str, original_result: dict, reason: str, mode: str
) -> dict[str, Any]:
    """§9.4 降级：记录 reason_code、downstream 用原文、正常返回原文路由。"""
    rewrite = RewriteResult(
        original_query=text,
        normalized_query=normalized,
        standalone_query=text,
        rewrite_type="none",
        changed=False,
        should_use=False,
        confidence=0.0,
        preserved_intent=True,
        reason_codes=[reason],
        model=RewriteModelInfo(provider="fallback", model_id="fallback", prompt_version=PROMPT_VERSION),
    )
    return {
        "mode": mode,
        "rewrite": rewrite,
        "original_route": original_result,
        "rewrite_route": None,
        "route_consistent": True,
        "downstream_query": text,
        "downstream_query_source": "original",
        "safety_decision": "fallback_original",
        "safety": None,
        "fallback_reason": reason,
        "final_route": original_result["route"],  # §7.4 恒为原文路由
        "cache_hit": False,
    }


def _decide_downstream(mode: str, safety: SafetyDecision, rewrite: RewriteResult) -> tuple[str, str, str]:
    """返回 (downstream_query, source, safety_decision)。

    只有 safe_apply 且安全门全绿且改写确实可用时才采用改写文本（§2.2 / §7.4）；
    shadow 与 normalize_only 均只影子评估不替换。
    """
    if safety.allow and mode == "safe_apply" and rewrite.should_use:
        return rewrite.standalone_query, "rewrite", "allow_rewrite"
    if safety.allow and mode in ("shadow", "normalize_only"):
        return rewrite.original_query, "original", "allow_rewrite_shadow"
    return rewrite.original_query, "original", safety.safety_decision


def _effect_of(result: dict | None) -> str:
    """路由结果 → 系统效果类型（Review 修复 §8.1）。

    route 是 effect_type 的兼容字段；优先读显式 effect_type，
    旧形状/桩结果退回 route，最终兜底 unclear（fail closed）。
    """
    if not isinstance(result, dict):
        return "unclear"
    return str(result.get("effect_type") or result.get("route") or "unclear")


def _intent_key(result: dict | None) -> str | None:
    """路由结果 → 业务意图 key（§9.1 起 intent 为 {key, name} 对象）。"""
    if not isinstance(result, dict):
        return None
    intent = result.get("intent")
    if isinstance(intent, dict):
        return str(intent.get("key") or "") or None
    return str(intent) if intent else None


def understand_query(
    db: Session,
    project_id: str,
    text: str,
    context: str | None,
    runtime,
    mode_override: str | None = None,
    predict_fn=None,
) -> dict[str, Any]:
    """双路分类 + 安全门 + downstream 决策（§9.3）。

    predict_fn(text, context) 返回标准路由结果 dict；缺省用共享推理运行时。
    """
    from app.services import inference_service  # 延迟导入避免循环依赖

    started = time.perf_counter()
    spec = get_project_rewrite_config(db, project_id)
    config = spec["config"]
    mode = mode_override if mode_override in VALID_MODES else config["mode"]

    normalized = normalize_text(text)
    if predict_fn is None:
        predict_fn = lambda t, c: inference_service.RUNTIME.predict_with(runtime, t, c)  # noqa: E731

    original_result = predict_fn(text, context)
    _metrics_inc("requests_total")

    if mode == "off":
        rewrite = _l0_result(text, normalized, _EMPTY_TERM)
        rewrite.should_use = False  # off 模式永不建议采用改写
        return _finalize(
            project_id, "off", text, context, rewrite,
            None, original_result, None, started,
            extra={
                "safety_decision": "mode_off",
                "downstream_query": text,
                "downstream_query_source": "original",
            },
        )

    # ---- L0 术语归一 ----
    term_result = apply_terminology(normalized, spec["terms"])
    term_targets = [r["target_term"] for r in term_result.replacements]

    def evaluate(candidate: str, changed: bool, provider_output: ProviderOutput | None):
        rewrite_route = predict_fn(candidate, None) if candidate != normalized else original_result
        # Review 修复 §8.1：安全门比较系统效果类型（服务端 Schema 映射结果），
        # 不比较业务标签名——自定义意图下标签名漂移不代表效果漂移
        safety = evaluate_rewrite_safety(
            original=text,
            context=context,
            rewrite=candidate,
            original_route=_effect_of(original_result),
            rewrite_route=_effect_of(rewrite_route),
            provider_output=provider_output,
            confidence_threshold=float(config["min_rewrite_confidence"]),
            require_route_consistency=bool(config.get("require_route_consistency", True)),
            term_targets=term_targets,
            changed=changed,
        )
        return rewrite_route, safety

    if mode == "normalize_only":
        rewrite = _l0_result(text, normalized, term_result)
        rewrite_route, safety = evaluate(term_result.text, rewrite.changed, None)
        return _finalize(
            project_id, mode, text, context, rewrite, safety, original_result, rewrite_route, started
        )

    # ---- shadow / safe_apply：L0 之后进入生成式改写（缓存 → provider） ----
    client = get_client()
    provider_spec = spec["provider"]
    connection_id = provider_spec["id"]
    cache_key = build_cache_key(
        project_id,
        spec["config_version_id"],
        spec["terminology_version_id"],
        PROMPT_VERSION,
        normalized,
        context,
        provider_connection_id=connection_id,
        provider_connection_revision=provider_spec.get("revision"),
        model_id=provider_spec.get("model_id"),
        generation_config_hash=provider_spec.get("generation_config_hash"),
        # Review 修复 §8.3：路由上下文入键，Schema/阈值变化后不复用旧安全摘要
        router_model_version_id=getattr(runtime, "model_version_id", None),
        router_schema_id=getattr(runtime, "schema_id", None),
        router_schema_hash=getattr(runtime, "schema_hash", None),
        router_threshold_version_id=getattr(runtime, "threshold_version_id", None),
    )
    cached = CACHE.get(cache_key)
    if cached is not None:
        _metrics_inc("cache_hit_total")
        rewrite = RewriteResult.model_validate(cached["rewrite"])
        # 安全门必须重评：路由结果可能随模型版本 / 阈值变化（缓存只复用生成结果）
        provider_output = ProviderOutput(
            standalone_query=rewrite.standalone_query,
            rewrite_type=rewrite.rewrite_type,
            should_use=rewrite.should_use,
            confidence=rewrite.confidence,
            preserved_intent=rewrite.preserved_intent,
        )
        rewrite_route, safety = evaluate(rewrite.standalone_query, rewrite.changed, provider_output)
        return _finalize(
            project_id, mode, text, context, rewrite, safety, original_result, rewrite_route,
            started, cache_hit=True,
        )

    out = _fast_context_rewrite(term_result.text, context)
    if out is not None:
        provider_name, provider_model, provider_latency = "rule_context", "L1", 0.0
        reply = None
    else:
        try:
            reply = client.rewrite(
                term_result.text,
                context,
                terminology=flatten_mapping(parse_terms(spec["terms"])) or None,
                timeout_ms=int(config.get("timeout_ms", get_settings().rewrite_timeout_ms)),
                provider_connection_id=connection_id if not provider_spec.get("builtin") else None,
                provider_connection_revision=provider_spec.get("revision"),
            )
        except ProviderTimeout as exc:
            reason = getattr(exc, "fallback_code", "TIMEOUT")
            _metrics_inc("fallback_total", reason)
            return _fallback_payload(text, normalized, original_result, reason, mode)
        except ProviderBusy:
            # V2 §3.3：有界队列满 → 立即回退原文（不重试、不计数为服务故障）
            _metrics_inc("fallback_total", "REWRITER_BUSY")
            return _fallback_payload(text, normalized, original_result, "REWRITER_BUSY", mode)
        except ProviderUnavailable as exc:
            # 外部模型 V1：AUTH/QUOTA/RATE_LIMIT 等细分原因码透出（§4.3 表）
            reason = getattr(exc, "fallback_code", "PROVIDER_UNAVAILABLE")
            _metrics_inc("fallback_total", reason)
            return _fallback_payload(text, normalized, original_result, reason, mode)
        except RewriteParseError:
            _metrics_inc("fallback_total", "INVALID_JSON")
            return _fallback_payload(text, normalized, original_result, "INVALID_JSON", mode)
        out = reply.output
        provider_name, provider_model, provider_latency = reply.provider, reply.model_id, reply.latency_ms

    standalone = out.standalone_query
    changed = standalone != normalized and standalone != text
    rtype = out.rewrite_type
    if term_result.changed and rtype in ("none", "normalization"):
        rtype = "mixed" if changed else "term_normalization"
    reason_codes = list(out.reason_codes)
    if term_result.changed and "NORMALIZED_TERM" not in reason_codes:
        reason_codes.append("NORMALIZED_TERM")
    rewrite = RewriteResult(
        original_query=text,
        normalized_query=normalized,
        standalone_query=standalone,
        rewrite_type=rtype,
        changed=changed,
        should_use=out.should_use and changed,
        confidence=out.confidence,
        preserved_intent=out.preserved_intent,
        mentioned_action=out.mentioned_action,
        objects=list(out.objects),
        constraints=out.constraints,
        missing_slots=list(out.missing_slots),
        assumptions=list(out.assumptions),
        used_context_refs=list(out.used_context_refs),
        reason_codes=reason_codes,
        model=RewriteModelInfo(provider=provider_name, model_id=provider_model, prompt_version=PROMPT_VERSION),
        latency_ms=provider_latency,
        term_replacements=term_result.replacements,
    )
    rewrite_route, safety = evaluate(standalone, changed, out)
    # 缓存只存结构化 RewriteResult 与安全摘要（§12.2；不存请求级延迟/调试信息）
    CACHE.put(
        cache_key,
        project_id,
        {"rewrite": rewrite.model_dump(), "safety": safety.model_dump()},
    )
    provider_trace = {
        "connection_id": connection_id,
        "connection_revision": provider_spec.get("revision"),
        "provider": provider_name,
        "model_id": provider_model,
        "provider_request_id": reply.request_id if reply is not None else None,
        "provider_latency_ms": provider_latency,
        "usage": reply.usage.model_dump() if reply is not None and reply.usage else None,
    }
    return _finalize(
        project_id, mode, text, context, rewrite, safety, original_result, rewrite_route, started,
        provider_trace=provider_trace,
    )


def _finalize(
    project_id: str,
    mode: str,
    text: str,
    context: str | None,
    rewrite: RewriteResult,
    safety: SafetyDecision | None,
    original_result: dict,
    rewrite_route: dict | None,
    started: float,
    cache_hit: bool = False,
    extra: dict[str, Any] | None = None,
    provider_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if safety is not None:
        downstream, source, decision = _decide_downstream(mode, safety, rewrite)
    else:
        downstream, source, decision = text, "original", "mode_off"

    # §17 可观测性：计数器 + 仅含 hash 的结构化日志（不落原文）
    _metrics_inc("success_total")
    if safety is not None and safety.route_conflict and rewrite_route is not None:
        _metrics_inc("route_conflict_total", f"{original_result['route']}->{rewrite_route['route']}")
    # Review 修复 §8.1：业务意图漂移单独记录（effect 一致仅代表效果层未漂移）
    intent_consistent = True if rewrite_route is None else _intent_key(original_result) == _intent_key(rewrite_route)
    if rewrite_route is not None and not intent_consistent:
        _metrics_inc("intent_drift_total", f"{_effect_of(original_result)}->{_effect_of(rewrite_route)}")
    if safety is not None:
        for code in safety.reason_codes:
            if code in (
                "NEGATION_CHANGED", "OBJECT_INVENTED", "ACTION_INTENSIFIED",
                "MODALITY_CHANGED", "ROUTE_CONFLICT", "LOW_CONFIDENCE", "UNSUPPORTED_ASSUMPTION",
            ):
                _metrics_inc("safety_reject_total", code)
    METRICS["latency_samples"].append(round((time.perf_counter() - started) * 1000, 1))
    logger.info(
        "rewrite project=%s hash=%s mode=%s decision=%s source=%s conf=%.2f codes=%s routes=%s/%s cache_hit=%s",
        project_id,
        text_hash(text, context)[:12],
        mode,
        decision,
        source,
        rewrite.confidence,
        rewrite.reason_codes,
        original_result["route"],
        rewrite_route["route"] if rewrite_route else "-",
        cache_hit,
    )
    payload: dict[str, Any] = {
        "mode": mode,
        "rewrite": rewrite,
        "original_route": original_result,
        "rewrite_route": rewrite_route,
        "route_consistent": (
            True
            if rewrite_route is None
            else original_result["route"] == rewrite_route["route"]
        ),
        "intent_consistent": intent_consistent,
        "downstream_query": downstream,
        "downstream_query_source": source,
        "safety_decision": decision,
        "safety": safety,
        "fallback_reason": None,
        "final_route": original_result["route"],  # §7.4 恒为原文路由
        "cache_hit": cache_hit,
        # 外部模型 V1 §9.4：Playground Trace（provider 元信息，不含密钥/原文）
        "provider_trace": provider_trace,
    }
    if extra:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------- 反馈与批量

def save_feedback(
    db: Session,
    project_id: str,
    input_hash: str,
    original_text: str | None,
    context: str | None,
    proposed_rewrite: str | None,
    edited_rewrite: str | None,
    verdict: str,
    reason_codes: list[str] | None,
    original_route: str | None,
    rewrite_route: str | None,
    model_id: str | None,
    prompt_version: str | None,
    store_raw_text: bool,
) -> RewriteFeedback:
    """§11.3：默认只保存 hash；原文仅在显式允许（store_raw_text）时保存。"""
    if db.get(Project, project_id) is None:
        raise NotFoundError("Project", project_id)
    if verdict not in ("accept", "reject", "edit"):
        raise ApiError("VALIDATION_ERROR", "verdict 必须是 accept/reject/edit", 422)
    row = RewriteFeedback(
        id=ids.prefixed(ids.REWRITE_FEEDBACK),
        project_id=project_id,
        input_hash=input_hash,
        original_text=original_text if store_raw_text else None,
        context=context if store_raw_text else None,
        proposed_rewrite=proposed_rewrite if store_raw_text else None,
        edited_rewrite=edited_rewrite if store_raw_text else None,
        verdict=verdict,
        reason_codes={"codes": reason_codes or []},
        original_route=original_route,
        rewrite_route=rewrite_route,
        model_id=model_id,
        prompt_version=prompt_version,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_feedback(db: Session, project_id: str, limit: int = 100) -> list[RewriteFeedback]:
    return (
        db.query(RewriteFeedback)
        .filter(RewriteFeedback.project_id == project_id)
        .order_by(RewriteFeedback.created_at.desc())
        .limit(limit)
        .all()
    )


def understand_batch(
    db: Session,
    project_id: str,
    items: list[dict],
    runtime,
    mode_override: str | None = None,
) -> dict:
    """§10.4：逐条降级，单条失败不中止批次；上限 max_batch_rewrite。"""
    settings = get_settings()
    if len(items) > settings.max_batch_rewrite:
        raise ApiError(
            "BATCH_TOO_LARGE",
            f"批量改写上限 {settings.max_batch_rewrite} 条",
            422,
            {"limit": settings.max_batch_rewrite},
        )
    results = []
    failed = 0
    for item in items:
        text = str(item.get("text", ""))
        context = item.get("context")
        try:
            payload = understand_query(db, project_id, text, context, runtime, mode_override=mode_override)
            if payload.get("safety_decision") == "fallback_original":
                failed += 1
        except Exception:  # 单条失败不中止批次
            failed += 1
            payload = _fallback_payload(
                text,
                normalize_text(text),
                {"route": "unknown", "decision": "unknown"},
                "PROVIDER_UNAVAILABLE",
                mode_override or "project_default",
            )
        results.append(payload)
    return {"count": len(results), "rewrite_failed_count": failed, "results": results}
