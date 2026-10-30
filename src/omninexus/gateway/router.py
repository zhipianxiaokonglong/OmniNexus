"""Model registry, capability negotiation, retry/fallback and streaming gateway."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from omninexus.config import AppConfig, ModelConfig, ProviderConfig
from omninexus.core.models import ChatDelta, ContentPart, LLMMessage
from omninexus.core.tokenizer import estimate_message_tokens, truncate_text
from omninexus.errors import (
    AllProvidersFailedError,
    ErrorCode,
    OmniError,
    ProviderError,
)
from omninexus.gateway.anthropic import AnthropicProvider
from omninexus.gateway.base import BaseProvider
from omninexus.gateway.cohere import CohereProvider
from omninexus.gateway.echo import EchoProvider
from omninexus.gateway.gemini import GeminiProvider
from omninexus.gateway.openai_compat import OpenAICompatibleProvider
from omninexus.logging import get_logger
from omninexus.providers.service import ProviderService, split_custom_ref

log = get_logger("gateway")

_PROVIDER_TYPES: dict[str, type[BaseProvider]] = {
    "openai": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "cohere": CohereProvider,
    "echo": EchoProvider,
}


@dataclass(slots=True)
class RoutePlan:
    requested_model: str
    primary: ModelConfig
    candidates: list[ModelConfig]
    effective_parts: list[ContentPart]
    degraded: bool = False
    capability_reason: str | None = None
    notes: list[str] = field(default_factory=list)
    selected_model: str | None = None
    selected_provider: str | None = None
    fallback_used: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def selected_model_config(self) -> ModelConfig:
        if not self.selected_model:
            return self.primary
        return next(
            (model for model in self.candidates if model.id == self.selected_model),
            self.primary,
        )


class GenerationInterruptedError(OmniError):
    def __init__(self, message: str) -> None:
        super().__init__(
            ErrorCode.GENERATION_INTERRUPTED,
            message,
            status_code=502,
            retryable=False,
        )


class _TokenBucket:
    def __init__(self, rpm: int) -> None:
        self.rpm = max(0, rpm)
        self.capacity = max(1, self.rpm)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self, max_wait_s: float) -> None:
        if self.rpm <= 0:
            return
        async with self.lock:
            now = time.monotonic()
            rate = self.rpm / 60.0
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * rate)
            self.updated = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / rate
                if wait > max_wait_s:
                    raise ProviderError(
                        "Local rate limit reached for this provider.",
                        code=ErrorCode.PROVIDER_RATE_LIMITED,
                        status_code=429,
                        retryable=False,
                    )
                await asyncio.sleep(wait)
                self.tokens = 0.0
                self.updated = time.monotonic()
            else:
                self.tokens -= 1


class Gateway:
    def __init__(
        self,
        config: AppConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        provider_service: ProviderService | None = None,
    ) -> None:
        self.config = config
        self._transport = transport
        self._provider_service = provider_service
        self._providers: dict[str, BaseProvider] = {}
        self._buckets: dict[str, _TokenBucket] = {}
        self._custom_names: set[str] = set()
        for name, provider_config in config.providers.items():
            cls = _PROVIDER_TYPES.get(provider_config.type)
            if cls is None:
                continue
            self._providers[name] = cls(
                name,
                provider_config,
                gateway=config.gateway,
                transport=transport,
            )
            override = provider_config.options.get("rate_limit_rpm")
            self._buckets[name] = _TokenBucket(
                int(override)
                if isinstance(override, (int, float))
                else config.gateway.rate_limit_rpm
            )

    async def refresh_custom_providers(self) -> None:
        if not self._provider_service:
            return
        desired: dict[str, BaseProvider] = {}
        for snapshot in self._provider_service.custom_snapshots():
            provider = snapshot.provider
            name = f"custom:{provider.id}"
            provider_config = ProviderConfig(
                type="openai",
                base_url=provider.base_url,
                api_key_env="",
                requires_key=False,
                enabled=provider.enabled,
                headers=provider.headers,
                connect_timeout_s=min(8.0, max(1.0, float(provider.timeout_seconds))),
            )
            desired[name] = OpenAICompatibleProvider(
                name,
                provider_config,
                gateway=self.config.gateway,
                transport=self._transport,
                api_key_override=snapshot.api_key,
            )
            if name not in self._buckets:
                self._buckets[name] = _TokenBucket(self.config.gateway.rate_limit_rpm)
        for name in list(self._custom_names - set(desired)):
            old = self._providers.pop(name, None)
            if old:
                await old.close()
                self._custom_names.discard(name)
        self._providers.update(desired)
        self._custom_names = set(desired)

    async def close(self) -> None:
        await asyncio.gather(
            *(provider.close() for provider in self._providers.values()),
            return_exceptions=True,
        )

    def provider(self, name: str) -> BaseProvider | None:
        return self._providers.get(name)

    def _chain(self, model_id: str) -> list[ModelConfig]:
        chain: list[ModelConfig] = []
        seen: set[str] = set()

        def walk(current_id: str, depth: int = 0) -> None:
            if depth > 8 or current_id in seen:
                return
            model = self.config.model_config_by_id(current_id)
            if not model:
                return
            seen.add(current_id)
            chain.append(model)
            for fallback in model.fallbacks:
                walk(fallback, depth + 1)

        walk(model_id)
        for fallback in self.config.gateway.fallback_models:
            walk(fallback)
        return chain

    @staticmethod
    def _supports(model: ModelConfig, parts: list[ContentPart]) -> bool:
        required = {part.modality for part in parts if part.modality != "text"}
        return required.issubset(set(model.input_modalities))

    @staticmethod
    def _degrade_part(part: ContentPart) -> ContentPart:
        metadata = part.metadata
        if part.modality == "image":
            alt = metadata.get("alt") or metadata.get("description") or metadata.get("ocr_text")
            name = metadata.get("filename") or metadata.get("name")
            if alt:
                text = f"[Image content (text description): {str(alt)[:2000]}]"
            elif name:
                text = f"[Image attachment omitted: {str(name)[:200]}]"
            else:
                text = "[An image was provided, but the selected model does not accept images.]"
        elif part.modality == "audio":
            transcript = metadata.get("transcript") or metadata.get("text")
            text = (
                f"[Audio transcript: {str(transcript)[:4000]}]"
                if transcript
                else (
                    "[An audio attachment was provided, but the selected model "
                    "does not accept audio.]"
                )
            )
        elif part.modality == "file":
            extracted = metadata.get("extracted_text") or metadata.get("text")
            name = metadata.get("filename") or metadata.get("name") or "file"
            text = (
                f"[File {str(name)[:200]} contents: {str(extracted)[:4000]}]"
                if extracted
                else f"[File attachment omitted: {str(name)[:200]}]"
            )
        else:
            text = f"[{part.modality} attachment omitted: unsupported modality.]"
        return ContentPart(
            modality="text",
            content=text,
            metadata={
                key: value
                for key, value in metadata.items()
                if key in ("alt", "description", "filename", "name", "mime_type")
            },
        )

    def _custom_model_config(self, provider_id: str, model) -> ModelConfig:
        return ModelConfig(
            id=f"custom:{provider_id}:{model.name}",
            provider=f"custom:{provider_id}",
            model=model.name,
            label=model.label,
            input_modalities=list(model.capabilities.input_modalities),
            output_modalities=["text"],
            context_window=model.capabilities.context_window,
            max_output_tokens=model.capabilities.max_output_tokens,
            supports_streaming=model.capabilities.supports_streaming,
            supports_tools=model.capabilities.supports_tools,
            metadata={"custom": True, "verification": model.verification},
        )

    def _plan_custom(self, model_id: str, parts: list[ContentPart]) -> RoutePlan:
        if not self._provider_service:
            raise OmniError(ErrorCode.UNKNOWN_MODEL, "Unknown model id.", status_code=404)
        split = split_custom_ref(model_id)
        if not split:
            raise OmniError(ErrorCode.UNKNOWN_MODEL, "Unknown model id.", status_code=404)
        provider_id, model_name = split
        snapshot = self._provider_service.get_custom(provider_id)
        if not snapshot:
            raise OmniError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "提供方已删除，请在聊天页重新选择。",
                status_code=409,
                details={"providerId": provider_id, "reason": "provider_deleted"},
            )
        provider = snapshot.provider
        if not provider.enabled:
            raise OmniError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "提供方已禁用，请在聊天页重新选择。",
                status_code=409,
                details={"providerId": provider_id, "reason": "provider_disabled"},
            )
        model = next((item for item in provider.models if item.name == model_name), None)
        if not model:
            raise OmniError(
                ErrorCode.MODEL_UNAVAILABLE,
                "模型已删除，请重新选择模型。",
                status_code=409,
                details={"providerId": provider_id, "model": model_name},
            )
        chosen = model
        effective_parts = list(parts)
        degraded = False
        reason: str | None = None
        notes: list[str] = []
        required = {part.modality for part in parts if part.modality != "text"}
        if required and not required.issubset(set(chosen.capabilities.input_modalities)):
            alternatives = [
                item
                for item in provider.models
                if required.issubset(set(item.capabilities.input_modalities))
            ]
            if alternatives:
                chosen = alternatives[0]
                reason = "capability_route"
                notes.append(f"routed within provider to {chosen.name} for {sorted(required)}")
            else:
                effective_parts = [self._degrade_part(part) for part in parts]
                degraded = True
                reason = "capability_degraded"
                notes.append(f"model {chosen.name} does not support {sorted(required)}; degraded")
        primary = self._custom_model_config(provider_id, chosen)
        return RoutePlan(
            requested_model=model_id,
            primary=primary,
            candidates=[primary],
            effective_parts=effective_parts,
            degraded=degraded,
            capability_reason=reason,
            notes=notes,
        )

    def plan(self, model_id: str, parts: list[ContentPart]) -> RoutePlan:
        if model_id.startswith("custom:"):
            return self._plan_custom(model_id, parts)
        model = self.config.model_config_by_id(model_id)
        if not model:
            raise OmniError(
                ErrorCode.UNKNOWN_MODEL,
                "Unknown model id.",
                status_code=404,
                details={"model": model_id},
            )
        chain = self._chain(model_id)
        if not chain:
            raise OmniError(ErrorCode.UNKNOWN_MODEL, "Unknown model id.", status_code=404)

        all_configured = [
            model
            for model in self.config.models
            if self.config.providers[model.provider].is_configured()
        ]
        configured = [candidate for candidate in chain if candidate in all_configured]
        global_reroute = False
        if not configured:
            # The requested chain has no usable key/endpoint. Rather than fail
            # while another configured model exists, reroute to one and mark it.
            if all_configured:
                chain = all_configured
                configured = all_configured
                global_reroute = True
            else:
                raise OmniError(
                    ErrorCode.NO_PROVIDER_AVAILABLE,
                    "No configured model is available. Add a provider key to .env "
                    "(or enable the offline echo model) and restart.",
                    status_code=503,
                    details={
                        "requested_model": model_id,
                        "models": [
                            {
                                "id": item.id,
                                "available": self.config.model_info(item).available,
                                "reason": self.config.model_info(item).unavailable_reason,
                            }
                            for item in chain[:8]
                        ],
                    },
                )

        base = chain[0] if chain[0] in configured else configured[0]
        effective_parts = list(parts)
        degraded = False
        reason: str | None = None
        notes: list[str] = []

        if global_reroute:
            reason = "requested_unavailable"
            notes.append(
                f"requested model chain for {model_id} has no configured provider; "
                f"using configured model {base.id}"
            )
        elif base is not chain[0]:
            reason = "requested_unavailable"
            notes.append(f"requested model {chain[0].id} is unavailable; using {base.id}")

        if not self._supports(base, parts):
            required = {p.modality for p in parts if p.modality != "text"}
            # Prefer a configured model that can accept the input, even if it is
            # not on the requested fallback chain.
            vision_capable = next(
                (item for item in all_configured if self._supports(item, parts)), None
            )
            if vision_capable is not None:
                base = vision_capable
                if vision_capable not in configured:
                    configured.append(vision_capable)
                # Keep other configured models as fallbacks; the gateway strips
                # images for any text-only candidate it has to use.
                for candidate in all_configured:
                    if candidate not in configured:
                        configured.append(candidate)
                reason = "capability_route"
                notes.append(f"routed to {base.id} for input modalities {sorted(required)}")
            else:
                effective_parts = [self._degrade_part(part) for part in parts]
                degraded = True
                reason = "capability_degraded"
                notes.append(f"model {base.id} lacks {sorted(required)}; degraded to text")

        if degraded and not any(p.modality == "text" for p in effective_parts):
            effective_parts.append(ContentPart(modality="text", content="[Unsupported attachment]"))

        # Order: chosen model, its own fallbacks, then the remaining configured chain.
        ordered: list[ModelConfig] = []
        seen: set[str] = set()
        for candidate in self._chain(base.id) + configured:
            if candidate.id not in seen and candidate in configured:
                seen.add(candidate.id)
                ordered.append(candidate)

        if not ordered:
            raise OmniError(
                ErrorCode.NO_PROVIDER_AVAILABLE,
                "No configured model is available.",
                status_code=503,
            )
        return RoutePlan(
            requested_model=model_id,
            primary=base,
            candidates=ordered,
            effective_parts=effective_parts,
            degraded=degraded,
            capability_reason=reason,
            notes=notes,
        )

    @staticmethod
    def _adapt_messages(messages: list[LLMMessage], model: ModelConfig) -> list[LLMMessage]:
        if "image" in model.input_modalities:
            return messages
        adapted: list[LLMMessage] = []
        for message in messages:
            if not message.images:
                adapted.append(LLMMessage(role=message.role, content=message.content))
                continue
            placeholders = []
            for image in message.images:
                placeholders.append(f"[Image omitted for {model.id}: {image.alt or 'no alt text'}]")
            content = "\n".join([message.content, *placeholders]).strip()
            adapted.append(LLMMessage(role=message.role, content=content))
        return adapted

    @staticmethod
    def _fit_messages(
        messages: list[LLMMessage], model: ModelConfig, reserve: int
    ) -> list[LLMMessage]:
        budget = max(1024, model.context_window - reserve)
        if not messages:
            return messages
        system = [m for m in messages if m.role == "system"]
        conversation = [m for m in messages if m.role != "system"]
        system = [
            LLMMessage(
                role="system",
                content=truncate_text(message.content, max(512, int(budget * 0.65)) * 3),
            )
            for message in system
        ]
        if not conversation:
            return system
        selected: list[LLMMessage] = []
        used = sum(estimate_message_tokens(m.content, len(m.images)) for m in system)
        for message in reversed(conversation):
            cost = estimate_message_tokens(message.content, len(message.images))
            if used + cost > budget and selected:
                break
            selected.append(message)
            used += cost
        selected.reverse()
        return system + selected

    async def stream(
        self,
        *,
        plan: RoutePlan,
        messages: list[LLMMessage],
        max_tokens: int,
        temperature: float,
        top_p: float,
    ) -> AsyncIterator[ChatDelta]:
        attempts: list[dict[str, Any]] = []
        emitted_any = False
        for index, candidate in enumerate(plan.candidates):
            provider = self._providers.get(candidate.provider)
            if provider is None:
                if candidate.provider.startswith("custom:"):
                    raise OmniError(
                        ErrorCode.PROVIDER_UNAVAILABLE,
                        "提供方不可用，请重新选择。",
                        status_code=409,
                    )
                attempts.append(
                    {
                        "provider": candidate.provider,
                        "model": candidate.id,
                        "code": "PROVIDER_UNAVAILABLE",
                    }
                )
                continue
            adapted = self._adapt_messages(messages, candidate)
            fitted = self._fit_messages(adapted, candidate, max_tokens + 512)
            bucket = self._buckets.setdefault(
                candidate.provider, _TokenBucket(self.config.gateway.rate_limit_rpm)
            )
            for attempt in range(self.config.gateway.max_retries + 1):
                attempt_info: dict[str, Any] = {
                    "provider": candidate.provider,
                    "model": candidate.id,
                    "attempt": attempt + 1,
                }
                started = time.monotonic()
                try:
                    await bucket.acquire(self.config.gateway.rate_limit_max_wait_s)
                    iterator = provider.stream(
                        model=candidate.model,
                        messages=fitted,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        top_p=top_p,
                    )
                    saw_content = False
                    try:
                        while True:
                            timeout = (
                                self.config.gateway.first_token_timeout_s
                                if not saw_content
                                else self.config.gateway.request_timeout_s
                            )
                            try:
                                async with asyncio.timeout(timeout):
                                    delta = await anext(iterator)
                            except StopAsyncIteration:
                                break
                            except TimeoutError as exc:
                                raise ProviderError(
                                    "Provider timed out before completing the response.",
                                    code=ErrorCode.PROVIDER_TIMEOUT,
                                    status_code=504,
                                    retryable=not saw_content,
                                    provider=candidate.provider,
                                    model=candidate.id,
                                ) from exc
                            if delta.text:
                                if not emitted_any:
                                    plan.selected_model = candidate.id
                                    plan.selected_provider = candidate.provider
                                    plan.fallback_used = index > 0
                                emitted_any = True
                                saw_content = True
                                yield delta
                            elif delta.usage:
                                yield delta
                    finally:
                        await iterator.aclose()
                    if not saw_content:
                        raise ProviderError(
                            "Provider returned no text content.",
                            code=ErrorCode.PROVIDER_BAD_RESPONSE,
                            status_code=502,
                            retryable=True,
                            provider=candidate.provider,
                            model=candidate.id,
                        )
                    attempt_info["duration_ms"] = int((time.monotonic() - started) * 1000)
                    attempts.append({**attempt_info, "ok": True})
                    plan.attempts = attempts
                    log.info(
                        "gateway_ok provider=%s model=%s attempt=%s duration_ms=%s",
                        candidate.provider,
                        candidate.id,
                        attempt + 1,
                        attempt_info["duration_ms"],
                        extra={
                            "provider": candidate.provider,
                            "model": candidate.id,
                            "duration_ms": attempt_info["duration_ms"],
                        },
                    )
                    return
                except ProviderError as exc:
                    if emitted_any:
                        # Cannot safely replay a half-delivered answer on another model.
                        plan.attempts = attempts
                        raise GenerationInterruptedError(
                            "The model stream was interrupted after partial output."
                        ) from exc
                    attempt_info.update(
                        {
                            "code": exc.code.value,
                            "status": exc.details.get("upstream_status"),
                            "retryable": exc.retryable,
                        }
                    )
                    attempts.append(attempt_info)
                    log.warning(
                        "gateway_attempt_failed provider=%s model=%s code=%s retryable=%s",
                        candidate.provider,
                        candidate.id,
                        exc.code.value,
                        exc.retryable,
                        extra={"provider": candidate.provider, "model": candidate.id},
                    )
                    if exc.retryable and attempt < self.config.gateway.max_retries:
                        backoff = min(
                            self.config.gateway.retry_max_backoff_s,
                            self.config.gateway.retry_backoff_s * (2**attempt),
                        )
                        if exc.retry_after_s is not None:
                            backoff = max(backoff, min(exc.retry_after_s, 10.0))
                        await asyncio.sleep(backoff + random.uniform(0, 0.2))
                        continue
                    break
                except Exception as exc:  # pragma: no cover - defensive boundary
                    attempts.append(
                        {
                            **attempt_info,
                            "code": "INTERNAL_ERROR",
                            "detail": type(exc).__name__,
                        }
                    )
                    log.error("gateway_internal_error type=%s", type(exc).__name__)
                    break
        plan.attempts = attempts
        raise AllProvidersFailedError(attempts)

    async def complete(
        self,
        *,
        plan: RoutePlan,
        messages: list[LLMMessage],
        max_tokens: int,
        temperature: float,
        top_p: float,
    ) -> tuple[str, Any, bool]:
        chunks: list[str] = []
        usage = None
        fallback_used = False
        async for delta in self.stream(
            plan=plan,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        ):
            chunks.append(delta.text)
            if delta.usage:
                usage = delta.usage
            if plan.fallback_used:
                fallback_used = True
        return "".join(chunks), usage, fallback_used
