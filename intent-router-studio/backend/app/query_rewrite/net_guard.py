"""自定义 Provider Base URL 的 SSRF 校验（外部模型 API 接入 V1 §10）。

- 默认只允许 https://；本地开发接 Ollama 等需显式设置
  REWRITE_ALLOW_PRIVATE_PROVIDER_URLS=true（UI 同时标红）
- 拒绝 userinfo、fragment、非 HTTP(S) scheme
- DNS 解析后拒绝 loopback / link-local / multicast / reserved / 私网 / metadata 地址
- 校验发生在两个时点：连接创建/更新时（net_guard.validate_provider_base_url）
  与每次真实请求前（ValidatingTransport.handle_request 重新解析，缓解
  DNS rebinding——攻击者必须在同一请求内完成二次绑定才能绕过）
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from app.errors import ApiError

METADATA_HOSTS = ("metadata.google.internal", "169.254.169.254")


def _bad(detail: str) -> ApiError:
    return ApiError("PROVIDER_URL_FORBIDDEN", f"Base URL 不允许: {detail}", 422)


def _allow_private() -> bool:
    from app.config import get_settings

    return bool(get_settings().rewrite_allow_private_provider_urls)


def _validate_ip(ip_text: str) -> None:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError as exc:
        raise _bad(f"无法解析 IP {ip_text}") from exc
    if (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_private or ip.is_unspecified):
        raise _bad(f"目标地址 {ip} 属于内网/保留/回环范围")


def resolve_and_validate(host: str, allow_private: bool | None = None) -> list[str]:
    """解析 host 并校验全部结果地址；返回 IP 文本列表。"""
    if allow_private is None:
        allow_private = _allow_private()
    if allow_private:
        return []
    if host.lower() in METADATA_HOSTS:
        raise _bad("云厂商 metadata 地址被拒绝")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise _bad(f"域名解析失败: {host}") from exc
    ips = sorted({info[4][0] for info in infos})
    for ip_text in ips:
        _validate_ip(ip_text)
    return ips


def validate_provider_base_url(url: str, allow_private: bool | None = None) -> str:
    """连接创建/更新时校验 Base URL；返回规范化 URL（结尾不带 /）。"""
    if allow_private is None:
        allow_private = _allow_private()
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise _bad("仅允许 http(s) scheme")
    if not parts.scheme.startswith("https") and not allow_private:
        raise _bad("默认仅允许 https://；本地服务请在部署开启 REWRITE_ALLOW_PRIVATE_PROVIDER_URLS")
    if not parts.hostname:
        raise _bad("缺少主机名")
    if parts.username or parts.password:
        raise _bad("URL 中不允许携带用户名/密码")
    if parts.fragment:
        raise _bad("URL 中不允许携带 fragment")
    if parts.query:
        raise _bad("URL 中不允许携带查询串")
    if parts.path and ".." in parts.path:
        raise _bad("路径中不允许相对路径片段")
    resolve_and_validate(parts.hostname, allow_private=allow_private)
    return url.rstrip("/")


class ValidatingTransport(httpx.HTTPTransport):
    """每次请求前重新解析并校验目标地址（缓解 DNS rebinding）。

    仅用于 openai_compatible 自定义 URL 的连接；GLM 固定官方端点不经此类。
    """

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self._allow_private = bool(kwargs.pop("allow_private", False))
        super().__init__(*args, **kwargs)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host:
            resolve_and_validate(host, allow_private=self._allow_private)
        return super().handle_request(request)
