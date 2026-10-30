"""Strict Base URL validation and joining.

Requirement: Base URL is used literally; we never auto-add ``/v1``.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

FORBIDDEN_SUFFIXES = ("/chat/completions", "/models")


class BaseUrlError(ValueError):
    pass


def validate_base_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise BaseUrlError("Base URL 不能为空")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BaseUrlError("Base URL 必须是 http(s) 绝对地址")
    if parsed.username or parsed.password:
        raise BaseUrlError("Base URL 不能包含用户名或密码，请使用 API Key 字段")
    if parsed.query or parsed.fragment:
        raise BaseUrlError("Base URL 不能包含查询参数或片段")
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    for suffix in FORBIDDEN_SUFFIXES:
        if lowered.endswith(suffix):
            raise BaseUrlError("Base URL 请填写到版本根路径，不要包含 /models 或 /chat/completions")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def join_base_url(base_url: str, endpoint: str) -> str:
    base = validate_base_url(base_url).rstrip("/")
    endpoint = endpoint.strip("/")
    return f"{base}/{endpoint}"


def models_url(base_url: str) -> str:
    return join_base_url(base_url, "models")


def chat_completions_url(base_url: str) -> str:
    return join_base_url(base_url, "chat/completions")


def bare_host_prompt(base_url: str) -> bool:
    """True when the URL has no meaningful path (front-end may suggest /v1)."""
    try:
        parsed = urlparse(validate_base_url(base_url))
    except BaseUrlError:
        return False
    return parsed.path in ("", "/")
