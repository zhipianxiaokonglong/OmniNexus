"""Provider abstraction shared by every vendor adapter."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from omninexus.config import GatewayConfig, ProviderConfig
from omninexus.core.models import ChatDelta, LLMMessage
from omninexus.errors import ErrorCode, ProviderError
from omninexus.logging import get_logger, register_secret

log = get_logger("gateway")

_DATA_URI = re.compile(r"^data:([^;,]+)?(;base64)?,(.*)$", re.S)


@dataclass(slots=True)
class ImagePayload:
    mime_type: str
    data: str
    is_base64: bool


def parse_image_url(url: str, fallback_mime: str = "image/png") -> ImagePayload | None:
    match = _DATA_URI.match(url)
    if not match:
        return None
    mime = (match.group(1) or fallback_mime).lower()
    return ImagePayload(mime_type=mime, data=match.group(3), is_base64=bool(match.group(2)))


def extract_error_message(_: str | None) -> str:
    # Upstream bodies can echo request content; never propagate them.
    return "The upstream model provider rejected the request."


async def map_http_error(
    response: httpx.Response,
    *,
    provider: str,
    model: str,
    body: str | None = None,
) -> ProviderError:
    status = response.status_code
    retry_after = None
    raw_retry = response.headers.get("retry-after")
    if raw_retry:
        try:
            retry_after = max(0.0, min(float(raw_retry), 60.0))
        except ValueError:
            retry_after = None

    if status in (401, 403):
        return ProviderError(
            "Provider authentication failed. Check the local API key configuration.",
            code=ErrorCode.PROVIDER_AUTH,
            status_code=502,
            retryable=False,
            provider=provider,
            model=model,
            upstream_status=status,
        )
    if status == 429:
        return ProviderError(
            "Provider rate limit reached.",
            code=ErrorCode.PROVIDER_RATE_LIMITED,
            status_code=503,
            retryable=True,
            retry_after_s=retry_after,
            provider=provider,
            model=model,
            upstream_status=status,
        )
    if status in (408, 504):
        return ProviderError(
            "Provider timed out.",
            code=ErrorCode.PROVIDER_TIMEOUT,
            status_code=504,
            retryable=True,
            retry_after_s=retry_after,
            provider=provider,
            model=model,
            upstream_status=status,
        )
    if status >= 500:
        return ProviderError(
            "Provider is temporarily unavailable.",
            code=ErrorCode.PROVIDER_ERROR,
            status_code=503,
            retryable=True,
            retry_after_s=retry_after,
            provider=provider,
            model=model,
            upstream_status=status,
        )
    return ProviderError(
        extract_error_message(body),
        code=ErrorCode.PROVIDER_ERROR,
        status_code=502,
        retryable=False,
        provider=provider,
        model=model,
        upstream_status=status,
    )


class BaseProvider(ABC):
    type_name = "base"

    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        *,
        gateway: GatewayConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        api_key_override: str | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self.gateway = gateway
        self._transport = transport
        self._api_key_override = api_key_override
        self._client: httpx.AsyncClient | None = None
        if config.api_key_env:
            register_secret(config.api_key())
        if api_key_override:
            register_secret(api_key_override)

    @property
    def api_key(self) -> str | None:
        if self._api_key_override is not None:
            return self._api_key_override
        key = self.config.api_key()
        register_secret(key)
        return key

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(
                self.gateway.request_timeout_s,
                connect=self.config.connect_timeout_s or self.gateway.connect_timeout_s,
                read=self.gateway.request_timeout_s,
                write=30.0,
                pool=10.0,
            )
            self._client = httpx.AsyncClient(
                timeout=timeout,
                transport=self._transport,
                follow_redirects=False,
                headers={"User-Agent": "OmniNexus/0.1 (+local)"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(self.config.headers)
        if extra:
            headers.update(extra)
        return headers

    def request_options(self) -> dict[str, Any]:
        return dict(self.config.options)

    @abstractmethod
    def stream(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        max_tokens: int,
        temperature: float,
        top_p: float,
    ) -> AsyncIterator[ChatDelta]:
        """Yield text deltas from the provider."""

    async def complete(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        max_tokens: int,
        temperature: float,
        top_p: float,
    ) -> tuple[str, Any]:
        parts: list[str] = []
        usage = None
        async for delta in self.stream(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        ):
            parts.append(delta.text)
            if delta.usage:
                usage = delta.usage
        return "".join(parts), usage


def parse_sse_json(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
