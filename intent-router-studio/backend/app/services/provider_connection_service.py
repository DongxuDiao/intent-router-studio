"""改写模型连接管理（外部模型 API 接入 V1 §7.1）。

- 系统级连接资源：内置 local_qwen 伪条目 + 远程连接（glm / openai_compatible）
- API Key 只写不读：接口永远只返回 has_api_key + api_key_hint
- 删除保护：内置不可删；被 ACTIVE 项目配置引用返回 409（含影响项目数）
- 显式测试连接：真实但固定的结构化探针，结果落连接状态，不进业务缓存
"""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from app import ids
from app.errors import ApiError, ConflictError, NotFoundError
from app.models import RewriteConfigVersion, RewriteProviderConnection
from app.models.tables import utcnow
from app.query_rewrite import credentials
from app.query_rewrite.glm_provider import GLM_BASE_URL
from app.query_rewrite.net_guard import validate_provider_base_url
from app.query_rewrite.remote_provider import GenerationConfig

logger = logging.getLogger("app.provider_connections")

BUILTIN_LOCAL_QWEN = "builtin:local_qwen"
BUILTIN_LOCAL_NAME = "本地 Qwen3-0.6B"
VALID_PROVIDER_TYPES = ("glm", "openai_compatible")

# 测试连接固定探针（V1 §7.1）：覆盖指代补全 + JSON mode + 阈值解析
TEST_PROBE_QUERY = "这个怎么停？"
TEST_PROBE_CONTEXT = "当前讨论实验 test-123"


def _log_safe(fields: dict[str, Any]) -> str:
    """结构化日志字段（白名单）：绝不含密钥/密文。"""
    return " ".join(f"{k}={v}" for k, v in fields.items())


def _build_remote_provider(row: RewriteProviderConnection) -> Any:
    """按连接行构造 Provider 实例（测试可 monkeypatch 本函数注入假实现）。"""
    from app.query_rewrite.glm_provider import GlmProvider
    from app.query_rewrite.net_guard import ValidatingTransport
    from app.query_rewrite.remote_provider import OpenAICompatibleProvider

    api_key = credentials.decrypt_api_key(
        row.api_key_ciphertext or "", row.api_key_nonce or "", row.id, row.revision
    )
    common = {
        "connection_id": row.id,
        "revision": row.revision,
        "model_id": row.model_id,
        "api_key": api_key,
        "generation_config": row.generation_config or {},
    }
    if row.provider_type == "glm":
        return GlmProvider(base_url=GLM_BASE_URL, **common)
    transport = ValidatingTransport(allow_private=_allow_private_urls())
    return OpenAICompatibleProvider(base_url=row.base_url, transport=transport, **common)


def _allow_private_urls() -> bool:
    from app.config import get_settings

    return bool(get_settings().rewrite_allow_private_provider_urls)


def _validate_generation_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    try:
        cfg = GenerationConfig(**(raw or {}))
    except Exception as exc:
        raise ApiError("VALIDATION_ERROR", f"generation_config 不合法: {exc}", 422) from exc
    return cfg.model_dump()


# ---------------------------------------------------------------- 查询

def _referenced_project_counts(db: Session) -> dict[str, int]:
    """ACTIVE 项目配置对各连接的引用数（连接为系统级资源，跨项目统计）。"""
    counts: dict[str, int] = {}
    rows = db.query(RewriteConfigVersion).filter(RewriteConfigVersion.status == "ACTIVE").all()
    seen: set[tuple[str, str]] = set()
    for row in rows:
        conn_id = (row.config or {}).get("provider_connection_id")
        if not conn_id or conn_id == BUILTIN_LOCAL_QWEN:
            continue
        key = (conn_id, row.project_id)
        if key in seen:
            continue
        seen.add(key)
        counts[conn_id] = counts.get(conn_id, 0) + 1
    return counts


def _to_dict(row: RewriteProviderConnection, in_use: int) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "provider_type": row.provider_type,
        "base_url": row.base_url,
        "model_id": row.model_id,
        "api_key_hint": row.api_key_hint,
        "has_api_key": bool(row.api_key_ciphertext),
        "generation_config": row.generation_config or {},
        "revision": row.revision,
        "enabled": bool(row.enabled),
        "egress_acknowledged": row.egress_acknowledged_at is not None,
        "last_test_status": row.last_test_status,
        "last_test_error_code": row.last_test_error_code,
        "last_test_latency_ms": row.last_test_latency_ms,
        "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
        "in_use_by_projects": in_use,
        "builtin": False,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def list_connections(db: Session) -> list[dict[str, Any]]:
    counts = _referenced_project_counts(db)
    items = [
        {
            "id": BUILTIN_LOCAL_QWEN,
            "name": BUILTIN_LOCAL_NAME,
            "provider_type": "local_qwen",
            "model_id": None,
            "builtin": True,
            "enabled": True,
            "egress_acknowledged": True,
            "in_use_by_projects": 0,
            "available": _builtin_available(),
        }
    ]
    rows = (
        db.query(RewriteProviderConnection)
        .order_by(RewriteProviderConnection.created_at.asc(), RewriteProviderConnection.id.asc())
        .all()
    )
    for row in rows:
        items.append(_to_dict(row, counts.get(row.id, 0)))
    return items


def _builtin_available() -> bool:
    """内置本地连接是否可用：rewriter 部署健康（失败也不阻塞列表）。"""
    try:
        from app.services.rewrite_service import deployment_info

        return bool(deployment_info().get("available"))
    except Exception:  # rewriter 不可达不影响列表
        return False


def get_connection(db: Session, connection_id: str) -> RewriteProviderConnection:
    if connection_id == BUILTIN_LOCAL_QWEN:
        raise ApiError("BUILTIN_CONNECTION_IMMUTABLE", "内置本地连接不支持该操作", 422)
    row = db.get(RewriteProviderConnection, connection_id)
    if row is None:
        raise NotFoundError("ProviderConnection", connection_id)
    return row


def get_connection_dict(db: Session, connection_id: str) -> dict[str, Any]:
    if connection_id == BUILTIN_LOCAL_QWEN:
        return next(item for item in list_connections(db) if item["id"] == BUILTIN_LOCAL_QWEN)
    counts = _referenced_project_counts(db)
    return _to_dict(get_connection(db, connection_id), counts.get(connection_id, 0))


def connection_snapshot(db: Session, connection_id: str) -> dict[str, Any]:
    """编排层轻量快照（外部模型 V1 §8.1 缓存键 / §7.2 selected_provider）。

    builtin 返回占位（模型身份随部署）；远程返回 revision/model/生成参数指纹
    与可用性。读取失败/不存在返回 available=False 而非抛错——编排层据此回退。
    """
    if not connection_id or connection_id == BUILTIN_LOCAL_QWEN:
        return {
            "id": BUILTIN_LOCAL_QWEN,
            "name": BUILTIN_LOCAL_NAME,
            "provider_type": "local_qwen",
            "model_id": None,
            "revision": None,
            "generation_config_hash": None,
            "builtin": True,
            "enabled": True,
            "egress_acknowledged": True,
            "last_test_status": None,
            "available": True,
        }
    try:
        row = db.get(RewriteProviderConnection, connection_id)
    except Exception:
        row = None
    if row is None:
        return {
            "id": connection_id, "name": connection_id, "provider_type": "unknown",
            "model_id": None, "revision": None, "generation_config_hash": None,
            "builtin": False, "enabled": False, "egress_acknowledged": False,
            "last_test_status": None, "available": False, "missing": True,
        }
    try:
        fingerprint = GenerationConfig(**(row.generation_config or {})).fingerprint()
    except Exception:
        fingerprint = "invalid"
    return {
        "id": row.id,
        "name": row.name,
        "provider_type": row.provider_type,
        "model_id": row.model_id,
        "revision": row.revision,
        "generation_config_hash": fingerprint,
        "builtin": False,
        "enabled": bool(row.enabled),
        "egress_acknowledged": row.egress_acknowledged_at is not None,
        "last_test_status": row.last_test_status,
        "available": bool(row.enabled) and bool(row.api_key_ciphertext),
    }


def validate_connection_for_config(db: Session, connection_id: str) -> None:
    """项目保存配置时的连接校验（外部模型 V1 §6.2）：存在、启用、已确认外发。"""
    snap = connection_snapshot(db, connection_id)
    if snap.get("missing"):
        raise ApiError("VALIDATION_ERROR", f"改写模型连接不存在: {connection_id}", 422)
    if not snap["enabled"]:
        raise ApiError("VALIDATION_ERROR", f"改写模型连接已禁用: {connection_id}", 422)
    if not snap["egress_acknowledged"]:
        raise ApiError(
            "EGRESS_NOT_ACKNOWLEDGED",
            f"连接 {connection_id} 未确认外部数据传输，不能选为项目改写模型",
            422,
        )


# ---------------------------------------------------------------- 创建 / 更新 / 删除

def create_connection(db: Session, payload: dict[str, Any]) -> RewriteProviderConnection:
    provider_type = payload.get("provider_type")
    if provider_type not in VALID_PROVIDER_TYPES:
        raise ApiError(
            "VALIDATION_ERROR",
            f"provider_type 必须是 {'/'.join(VALID_PROVIDER_TYPES)} 之一",
            422,
        )
    name = str(payload.get("name") or "").strip()
    if not 1 <= len(name) <= 100:
        raise ApiError("VALIDATION_ERROR", "name 长度须在 1~100", 422)
    model_id = str(payload.get("model_id") or "").strip()
    if not 1 <= len(model_id) <= 200:
        raise ApiError("VALIDATION_ERROR", "model_id 必填（如 glm-5.2）", 422)
    if not payload.get("egress_acknowledged"):
        raise ApiError(
            "EGRESS_NOT_ACKNOWLEDGED",
            "创建远程连接前必须确认：改写请求中的 Query、上下文和术语可能被发送到该外部模型服务",
            422,
        )
    api_key = str(payload.get("api_key") or "")
    if len(api_key) < 8:
        raise ApiError("VALIDATION_ERROR", "api_key 过短", 422)

    # GLM 的 Base URL 由后端固定为官方端点；自定义 URL 走 openai_compatible
    if provider_type == "glm":
        base_url = GLM_BASE_URL
    else:
        base_url = validate_provider_base_url(str(payload.get("base_url") or ""))
        if not base_url:
            raise ApiError("VALIDATION_ERROR", "openai_compatible 类型必须提供 base_url", 422)

    generation_config = _validate_generation_config(payload.get("generation_config"))
    if not credentials.master_key_configured():
        raise credentials.CredentialError("CREDENTIAL_ENCRYPTION_NOT_CONFIGURED", "未配置凭据主密钥", 503)

    connection_id = ids.prefixed(ids.PROVIDER_CONNECTION)
    ciphertext, nonce = credentials.encrypt_api_key(api_key, connection_id, revision=1)
    row = RewriteProviderConnection(
        id=connection_id,
        name=name,
        provider_type=provider_type,
        base_url=base_url,
        model_id=model_id,
        api_key_ciphertext=ciphertext,
        api_key_nonce=nonce,
        api_key_hint=credentials.key_hint(api_key),
        generation_config=generation_config,
        capabilities={"protocol": "openai_chat_completions"},
        revision=1,
        enabled=True,
        egress_acknowledged_at=utcnow(),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "provider_connection_created %s",
        _log_safe({"id": row.id, "provider": row.provider_type, "model": row.model_id, "revision": 1}),
    )
    return row


def update_connection(db: Session, connection_id: str, payload: dict[str, Any]) -> RewriteProviderConnection:
    row = get_connection(db, connection_id)
    affects_output = False
    affects_auth = False

    name = payload.get("name")
    if name is not None:
        name = str(name).strip()
        if not 1 <= len(name) <= 100:
            raise ApiError("VALIDATION_ERROR", "name 长度须在 1~100", 422)
        row.name = name

    model_id = payload.get("model_id")
    if model_id is not None:
        model_id = str(model_id).strip()
        if not 1 <= len(model_id) <= 200:
            raise ApiError("VALIDATION_ERROR", "model_id 不能为空", 422)
        if model_id != row.model_id:
            row.model_id = model_id
            affects_output = True

    base_url = payload.get("base_url")
    if base_url is not None:
        if row.provider_type == "glm":
            raise ApiError("VALIDATION_ERROR", "GLM 连接的 Base URL 固定为官方端点，不可修改", 422)
        new_url = validate_provider_base_url(str(base_url))
        if new_url != row.base_url:
            row.base_url = new_url
            affects_output = True

    generation_config = payload.get("generation_config")
    if generation_config is not None:
        new_cfg = _validate_generation_config(generation_config)
        if new_cfg != (row.generation_config or {}):
            row.generation_config = new_cfg
            affects_output = True

    enabled = payload.get("enabled")
    if enabled is not None:
        row.enabled = bool(enabled)

    api_key = payload.get("api_key")
    key_changed = False
    if api_key is not None and str(api_key) != "":
        if len(str(api_key)) < 8:
            raise ApiError("VALIDATION_ERROR", "api_key 过短", 422)
        key_changed = True
        affects_auth = True

    if affects_output or affects_auth:
        old_revision = row.revision
        new_revision = old_revision + 1
        # AAD 绑定 revision：revision 变化必须重加密（新 Key 或旧 Key 均如此）
        plain = (
            str(api_key)
            if key_changed
            else credentials.decrypt_api_key(
                row.api_key_ciphertext or "", row.api_key_nonce or "", row.id, old_revision
            )
        )
        ciphertext, nonce = credentials.encrypt_api_key(plain, row.id, new_revision)
        row.api_key_ciphertext = ciphertext
        row.api_key_nonce = nonce
        row.api_key_hint = credentials.key_hint(plain) if key_changed else row.api_key_hint
        row.revision = new_revision
        row.last_test_status = None
        row.last_test_error_code = None
        row.last_test_latency_ms = None
        row.last_tested_at = None

    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    _notify_connection_changed(row)
    logger.info(
        "provider_connection_updated %s",
        _log_safe({"id": row.id, "revision": row.revision, "output_changed": affects_output, "auth_changed": affects_auth}),
    )
    return row


def delete_connection(db: Session, connection_id: str) -> None:
    if connection_id == BUILTIN_LOCAL_QWEN:
        raise ApiError("BUILTIN_CONNECTION_IMMUTABLE", "内置本地连接不可删除", 422)
    row = get_connection(db, connection_id)
    counts = _referenced_project_counts(db)
    affected = counts.get(connection_id, 0)
    if affected > 0:
        raise ConflictError(
            "PROVIDER_CONNECTION_IN_USE",
            f"连接被 {affected} 个项目的生效改写配置引用，先切换这些项目到其他连接",
            {"affected_projects": affected},
        )
    provider, model = row.provider_type, row.model_id
    db.delete(row)
    db.commit()
    # 通知仍携带连接 ID（行已删除，用轻量对象），各进程据此清缓存与熔断
    from types import SimpleNamespace

    _notify_connection_changed(SimpleNamespace(id=connection_id, provider_type=provider, model_id=model))
    logger.info("provider_connection_deleted %s", _log_safe({"id": connection_id, "provider": provider, "model": model}))


def clear_credential(db: Session, connection_id: str, confirm: bool) -> RewriteProviderConnection:
    """DELETE /credential：单独操作清除密钥（需二次确认），连接保留但禁用。"""
    if not confirm:
        raise ApiError("CONFIRM_REQUIRED", "清除密钥需要显式确认（confirm=true）", 422)
    row = get_connection(db, connection_id)
    row.api_key_ciphertext = None
    row.api_key_nonce = None
    row.api_key_hint = "****"
    row.enabled = False  # 无密钥的远程连接不可用
    row.revision += 1
    row.last_test_status = None
    row.last_test_error_code = None
    row.last_test_latency_ms = None
    row.last_tested_at = None
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    _notify_connection_changed(row)
    logger.info("provider_connection_credential_cleared %s", _log_safe({"id": row.id, "revision": row.revision}))
    return row


# ---------------------------------------------------------------- 显式测试

def test_connection(db: Session, connection_id: str) -> dict[str, Any]:
    """真实结构化探针（收费请求）；结果写连接状态，不改项目配置、不进业务缓存。"""
    row = get_connection(db, connection_id)
    if row.egress_acknowledged_at is None:
        raise ApiError("EGRESS_NOT_ACKNOWLEDGED", "未确认外部数据传输的连接不可测试", 422)
    if not row.enabled:
        raise ApiError("PROVIDER_CONNECTION_DISABLED", "连接已禁用，先启用再测试", 422)
    if not row.api_key_ciphertext:
        raise ApiError("CREDENTIAL_MISSING", "连接没有 API Key，先补录密钥", 422)

    # 外部 Flash 模型在冷启动或繁忙时可能超过 15 秒。测试预算跟随连接的
    # read_timeout_ms，额外留 1 秒连接/解析余量，同时封顶 120 秒。
    cfg = GenerationConfig(**(row.generation_config or {}))
    test_timeout_ms = min(120_000, max(15_000, cfg.read_timeout_ms + 1_000))
    started = time.perf_counter()
    try:
        provider = _build_remote_provider(row)
        reply = provider.rewrite(TEST_PROBE_QUERY, TEST_PROBE_CONTEXT, None, timeout_ms=test_timeout_ms)
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        row.last_test_status = "SUCCESS"
        row.last_test_error_code = None
        row.last_test_latency_ms = latency_ms
        row.last_tested_at = utcnow()
        db.commit()
        _notify_test_succeeded(row)
        logger.info(
            "provider_connection_test_ok %s",
            _log_safe({"id": row.id, "latency_ms": latency_ms, "provider_request_id": reply.request_id}),
        )
        return {
            "status": "SUCCESS",
            "latency_ms": latency_ms,
            "provider": reply.provider,
            "model_id": reply.model_id,
            "provider_request_id": reply.request_id,
            "usage": reply.usage.model_dump() if reply.usage else None,
            "standalone_query": reply.output.standalone_query[:200],
        }
    except Exception as exc:  # 测试失败不抛给调用方：结果落库展示
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        code = getattr(exc, "fallback_code", None) or "PROVIDER_UNAVAILABLE"
        row.last_test_status = "FAILED"
        row.last_test_error_code = code
        row.last_test_latency_ms = latency_ms
        row.last_tested_at = utcnow()
        db.commit()
        _notify_test_failed(row)
        logger.warning(
            "provider_connection_test_failed %s",
            _log_safe({"id": row.id, "code": code, "latency_ms": latency_ms, "exc": type(exc).__name__}),
        )
        return {"status": "FAILED", "error_code": code, "latency_ms": latency_ms, "message": str(exc)[:200]}


# ---------------------------------------------------------------- 变更通知（阶段3 registry 挂钩）

_LISTENERS: list = []


def register_listener(callback) -> None:
    """registry 注册连接变更回调（更新/删除/测试后清缓存与熔断）。"""
    if callback not in _LISTENERS:
        _LISTENERS.append(callback)


def _notify_connection_changed(row: RewriteProviderConnection | None) -> None:
    for callback in list(_LISTENERS):
        try:
            callback("changed", row)
        except Exception:  # 通知失败不影响业务
            logger.exception("provider connection listener failed")


def _notify_test_succeeded(row: RewriteProviderConnection) -> None:
    for callback in list(_LISTENERS):
        try:
            callback("test_ok", row)
        except Exception:
            logger.exception("provider connection listener failed")


def _notify_test_failed(row: RewriteProviderConnection) -> None:
    for callback in list(_LISTENERS):
        try:
            callback("test_failed", row)
        except Exception:
            logger.exception("provider connection listener failed")
