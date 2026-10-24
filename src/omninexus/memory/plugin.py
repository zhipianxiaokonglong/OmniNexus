"""Reserved memory extension: plugin interface, disabled default and HTTP adapter.

The product's official memory path is the local archive + recent-window context.
This plugin is kept for a future local memory pipeline, defaults to disabled and
must not be deleted. Its health probe always reports ``unavailable`` while
disabled, and the main conversation path never waits on it.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from omninexus.config import MemoryConfig
from omninexus.logging import get_logger, register_secret
from omninexus.memory.contract import (
    CONTRACT_HEADER,
    ERROR_CODES,
    MEMORY_CONTRACT_VERSION,
    MemoryQueryResponse,
    build_query,
)

log = get_logger("memory")


@dataclass(slots=True)
class MemoryHealth:
    state: str  # disabled | unavailable | ok | circuit_open
    configured: bool
    reason: str = ""
    latency_ms: int | None = None
    last_check: float | None = None
    contract_version: str = MEMORY_CONTRACT_VERSION

    @property
    def available(self) -> bool:
        return self.state == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "available": self.available,
            "configured": self.configured,
            "reason": self.reason,
            "latency_ms": self.latency_ms,
            "last_check": self.last_check,
            "contract_version": self.contract_version,
            "reserved": True,
        }


@dataclass(slots=True)
class MemoryQueryResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    available: bool = False
    reason: str = ""
    latency_ms: int | None = None
    tokens: int = 0

    @property
    def used(self) -> bool:
        return bool(self.items)


class MemoryPlugin(ABC):
    """Stable extension surface for a future local memory pipeline."""

    reserved = True

    @abstractmethod
    async def health(self, *, force: bool = False) -> MemoryHealth:
        raise NotImplementedError

    @abstractmethod
    async def query(self, text: str, *, session_id: str | None = None) -> MemoryQueryResult:
        raise NotImplementedError

    @abstractmethod
    async def writeback(
        self,
        *,
        session_id: str,
        event_id: str,
        items: list[dict[str, Any]],
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

    def status(self) -> MemoryHealth:
        return MemoryHealth(state="unavailable", configured=False, reason="not_initialized")


class DisabledMemoryPlugin(MemoryPlugin):
    """Zero-I/O implementation used when the reserved extension is disabled."""

    def __init__(self, reason: str = "disabled") -> None:
        self.reason = reason

    async def health(self, *, force: bool = False) -> MemoryHealth:
        # v1.1: the reserved probe always reports unavailable while not connected.
        return MemoryHealth(state="unavailable", configured=False, reason=self.reason)

    async def query(self, text: str, *, session_id: str | None = None) -> MemoryQueryResult:
        return MemoryQueryResult(available=False, reason=self.reason)

    async def writeback(
        self, *, session_id: str, event_id: str, items: list[dict[str, Any]]
    ) -> bool:
        return False

    async def close(self) -> None:
        return None

    def status(self) -> MemoryHealth:
        return MemoryHealth(state="unavailable", configured=False, reason=self.reason)


class HttpMemoryPlugin(MemoryPlugin):
    """HTTP adapter for a future memory service implementing contract v1.0."""

    def __init__(
        self,
        config: MemoryConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._state = "unavailable"
        self._reason = "not_probed"
        self._last_check: float | None = None
        self._latency_ms: int | None = None
        self._failures = 0
        self._open_until = 0.0
        token = self._token()
        if token:
            register_secret(token)

    @property
    def configured(self) -> bool:
        return self.config.is_configured

    def _token(self) -> str | None:
        if not self.config.auth_token_env:
            return None
        import os

        token = os.environ.get(self.config.auth_token_env)
        return token.strip() if token else None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            CONTRACT_HEADER: self.config.contract_version,
        }
        token = self._token()
        if token:
            register_secret(token)
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(
                max(self.config.query_timeout_ms, self.config.health_timeout_ms) / 1000,
                connect=min(self.config.health_timeout_ms, 1000) / 1000,
            )
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url,
                timeout=timeout,
                transport=self._transport,
                follow_redirects=False,
                headers={"User-Agent": "OmniNexus/0.1 (reserved-memory)"},
            )
        return self._client

    def status(self) -> MemoryHealth:
        return MemoryHealth(
            state=self._state,
            configured=self.configured,
            reason=self._reason,
            latency_ms=self._latency_ms,
            last_check=self._last_check,
            contract_version=self.config.contract_version,
        )

    def _record_failure(self, reason: str) -> None:
        self._failures += 1
        self._state = "unavailable"
        self._reason = reason
        self._last_check = time.time()
        if self._failures >= self.config.circuit_failures:
            self._state = "circuit_open"
            self._open_until = time.monotonic() + self.config.circuit_cooldown_s
            self._reason = "circuit_open"

    def _record_success(self, latency_ms: int) -> None:
        self._failures = 0
        self._open_until = 0.0
        self._state = "ok"
        self._reason = ""
        self._latency_ms = latency_ms
        self._last_check = time.time()

    async def health(self, *, force: bool = False) -> MemoryHealth:
        if not self.configured:
            self._state = "unavailable"
            self._reason = "disabled" if not self.config.enabled else "not_configured"
            return self.status()
        if self._open_until and time.monotonic() < self._open_until:
            self._state = "circuit_open"
            self._reason = "circuit_open"
            return self.status()
        if (
            not force
            and self._last_check is not None
            and time.time() - self._last_check < self.config.health_interval_s
        ):
            return self.status()

        async with self._lock:
            if (
                not force
                and self._last_check is not None
                and time.time() - self._last_check < self.config.health_interval_s
            ):
                return self.status()
            started = time.monotonic()
            try:
                timeout = self.config.health_timeout_ms / 1000
                async with asyncio.timeout(timeout):
                    response = await self._client_or_create().get(
                        self.config.health_path, headers=self._headers()
                    )
                latency = int((time.monotonic() - started) * 1000)
                if response.status_code != 200:
                    self._record_failure(f"http_{response.status_code}")
                    return self.status()
                data = response.json()
                if not isinstance(data, dict) or str(data.get("status", "")).lower() not in {
                    "ok",
                    "healthy",
                    "ready",
                }:
                    self._record_failure("bad_health")
                    return self.status()
                version = str(data.get("contract_version", ""))
                if version and not version.startswith("1."):
                    self._record_failure("contract_mismatch")
                    return self.status()
                self._record_success(latency)
            except TimeoutError:
                self._record_failure("health_timeout")
            except (httpx.HTTPError, ValueError):
                self._record_failure("health_unreachable")
            except Exception:  # pragma: no cover - defensive boundary
                self._record_failure("health_error")
            return self.status()

    async def query(self, text: str, *, session_id: str | None = None) -> MemoryQueryResult:
        if not self.configured:
            return MemoryQueryResult(available=False, reason="disabled")
        health = await self.health()
        if not health.available:
            return MemoryQueryResult(available=False, reason=health.state)

        envelope = build_query(text, top_k=self.config.top_k)
        started = time.monotonic()
        try:
            timeout = self.config.query_timeout_ms / 1000
            async with asyncio.timeout(timeout):
                response = await self._client_or_create().post(
                    self.config.query_path, json=envelope, headers=self._headers()
                )
            latency = int((time.monotonic() - started) * 1000)
            if response.status_code != 200:
                self._record_failure(f"http_{response.status_code}")
                return MemoryQueryResult(
                    available=False, reason=f"http_{response.status_code}", latency_ms=latency
                )
            data = response.json()
            if not isinstance(data, dict):
                self._record_failure("bad_schema")
                return MemoryQueryResult(available=False, reason="bad_schema")
            code = data.get("code")
            if code and code != "MEM_OK":
                self._record_failure(str(code))
                return MemoryQueryResult(available=False, reason=str(code))
            parsed = MemoryQueryResponse.model_validate(data)
            if parsed.contract_version and not parsed.contract_version.startswith("1."):
                self._record_failure("contract_mismatch")
                return MemoryQueryResult(available=False, reason="contract_mismatch")
            items = [item.model_dump() for item in parsed.items[: self.config.max_items]]
            self._record_success(latency)
            return MemoryQueryResult(
                items=items,
                available=True,
                latency_ms=latency,
                tokens=parsed.tokens,
            )
        except TimeoutError:
            self._record_failure("query_timeout")
            return MemoryQueryResult(available=False, reason="query_timeout")
        except (httpx.HTTPError, ValueError):
            self._record_failure("query_failed")
            return MemoryQueryResult(available=False, reason="query_failed")
        except Exception:  # pragma: no cover - defensive boundary
            self._record_failure("query_error")
            return MemoryQueryResult(available=False, reason="query_error")

    async def writeback(
        self, *, session_id: str, event_id: str, items: list[dict[str, Any]]
    ) -> bool:
        if not self.configured:
            return False
        health = await self.health()
        if not health.available:
            return False
        payload = {
            "contract_version": self.config.contract_version,
            "session_id": session_id,
            "event_id": event_id,
            "items": items[:16],
        }
        try:
            timeout = self.config.write_timeout_ms / 1000
            async with asyncio.timeout(timeout):
                response = await self._client_or_create().post(
                    self.config.write_path, json=payload, headers=self._headers()
                )
            if response.status_code in (200, 201, 202):
                return True
            self._record_failure(f"write_http_{response.status_code}")
            return False
        except (TimeoutError, httpx.HTTPError, ValueError):
            self._record_failure("write_failed")
            return False
        except Exception:  # pragma: no cover
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def public_base_url(self) -> str:
        """Return a credential-free URL for status endpoints."""
        parts = urlsplit(self.config.base_url)
        host = parts.hostname or ""
        netloc = host if parts.port is None else f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def build_memory_plugin(
    config: MemoryConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> MemoryPlugin:
    if not config.enabled:
        return DisabledMemoryPlugin("disabled")
    if not config.is_configured:
        return DisabledMemoryPlugin("not_configured")
    return HttpMemoryPlugin(config, transport=transport)


def error_code_status(code: str) -> int:
    return ERROR_CODES.get(code, 500)
