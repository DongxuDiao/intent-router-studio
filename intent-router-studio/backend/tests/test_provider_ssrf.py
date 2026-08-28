"""SSRF 校验测试（外部模型 API 接入 V1 §10）。"""
from __future__ import annotations

import ipaddress
import socket

import httpx
import pytest

from app.errors import ApiError
from app.query_rewrite import net_guard


def _resolve_public(_host, _port=None):
    # 公网域名（如 open.bigmodel.cn）解析出的合法地址
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.30.10", 0))]


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: _resolve_public(host))
    monkeypatch.setenv("REWRITE_ALLOW_PRIVATE_PROVIDER_URLS", "false")


def _patch_resolve(monkeypatch, ips: list[str]):
    def fake(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.mark.parametrize(
    "bad_ip",
    ["127.0.0.1", "10.1.2.3", "192.168.1.4", "172.16.0.9", "169.254.169.254", "fd00::1", "224.0.0.1", "0.0.0.0"],
)
def test_private_and_reserved_ips_rejected(monkeypatch, bad_ip):
    _patch_resolve(monkeypatch, [bad_ip])
    with pytest.raises(ApiError) as exc:
        net_guard.validate_provider_base_url("https://api.example.com/v1")
    assert exc.value.code == "PROVIDER_URL_FORBIDDEN"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://api.example.com/v1",
        "file:///etc/passwd",
        "https://user:pass@api.example.com/v1",
        "https://api.example.com/v1#frag",
        "https://api.example.com/v1?q=1",
        "https://api.example.com/../etc",
        "not a url",
        "",
    ],
)
def test_malformed_urls_rejected(url):
    with pytest.raises(ApiError):
        net_guard.validate_provider_base_url(url)


def test_http_rejected_without_private_switch():
    with pytest.raises(ApiError) as exc:
        net_guard.validate_provider_base_url("http://ollama.local:11434/v1")
    assert "https" in exc.value.message


def test_private_switch_allows_http_and_private(monkeypatch):
    monkeypatch.setenv("REWRITE_ALLOW_PRIVATE_PROVIDER_URLS", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert net_guard.validate_provider_base_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"
    finally:
        get_settings.cache_clear()


def test_valid_https_url_normalizes_trailing_slash():
    assert (
        net_guard.validate_provider_base_url("https://api.example.com/v1/")
        == "https://api.example.com/v1"
    )


def test_metadata_hostname_rejected(monkeypatch):
    _patch_resolve(monkeypatch, ["169.254.169.254"])
    with pytest.raises(ApiError):
        net_guard.validate_provider_base_url("https://metadata.google.internal/v1")


def test_transport_revalidates_each_request(monkeypatch):
    """DNS rebinding：校验通过后域名被重绑到私网，下一次请求必须在校验层被拦下。"""
    current = {"ips": ["104.18.30.10"]}

    def flip(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in current["ips"]]

    monkeypatch.setattr(socket, "getaddrinfo", flip)
    transport = net_guard.ValidatingTransport(allow_private=False)
    request = httpx.Request("POST", "https://rebind.example.com/v1/chat/completions", json={})

    # 第一次：公网地址通过校验（连接层失败可接受——没有真实服务器）
    try:
        transport.handle_request(request)
    except (OSError, httpx.HTTPError):
        pass
    # 重绑到私网：transport 在发起连接前拦截
    current["ips"] = ["10.0.0.5"]
    with pytest.raises(ApiError):
        transport.handle_request(request)


def test_transport_allows_public_ip(monkeypatch):
    _patch_resolve(monkeypatch, ["104.18.30.10"])
    transport = net_guard.ValidatingTransport(allow_private=False)
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions", json={})
    # 公网地址通过校验后进入真实 HTTP 传输层（example.com 可达或连接失败均非 SSRF 错误）
    try:
        transport.handle_request(request)
    except (OSError, httpx.HTTPError):
        pass  # 网络层错误不是校验失败


def test_ipaddress_classification_selfcheck():
    assert ipaddress.ip_address("104.18.30.10").is_global
    assert not ipaddress.ip_address("8.8.8.8").is_private
