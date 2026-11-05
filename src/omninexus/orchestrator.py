"""Turn orchestration: input normalization, archive, context, retrieval, gateway."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from omninexus.config import AppConfig
from omninexus.core.classifier import classify
from omninexus.core.emotions import tag_contract
from omninexus.core.models import (
    Citation,
    InboundEvent,
    NormalizedInput,
    OutputEvent,
    OutputTags,
    SessionRecord,
    StoredMessage,
    TokenUsage,
    iso_now,
)
from omninexus.core.tokenizer import estimate_tokens
from omninexus.errors import (
    ErrorCode,
    InvalidInputError,
    OmniError,
    ProviderError,
)
from omninexus.gateway.router import Gateway, GenerationInterruptedError, RoutePlan
from omninexus.logging import get_logger
from omninexus.memory.plugin import MemoryPlugin, MemoryQueryResult
from omninexus.prompts.manager import PromptManager
from omninexus.providers.service import ProviderService
from omninexus.search.service import SearchOutcome, SearchService
from omninexus.sessions.service import (
    ContextBuilder,
    SessionService,
    append_citations,
    compose_system_prompt,
    context_budget,
)
from omninexus.storage.database import Database

log = get_logger("orchestrator")


@dataclass(slots=True)
class StreamFrame:
    event: str
    data: dict[str, Any]


@dataclass(slots=True)
class PreparedTurn:
    input_event_id: str
    output_event_id: str
    session: SessionRecord
    user_text: str
    plan: RoutePlan
    messages: list[Any]
    system_prompt: str
    prompt_profile: str
    web_search_requested: bool
    search_outcome: SearchOutcome
    memory_result: MemoryQueryResult
    max_tokens: int
    temperature: float
    top_p: float
    replay: StoredMessage | None = None
    dropped_messages: int = 0
    session_created: bool = False
    legacy_session_id: bool = False


@dataclass(slots=True)
class TurnResult:
    output: OutputEvent
    citations: list[Citation] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    usage: TokenUsage = field(default_factory=TokenUsage)
    replay: bool = False


class Orchestrator:
    def __init__(
        self,
        *,
        config: AppConfig,
        db: Database,
        gateway: Gateway,
        prompts: PromptManager,
        sessions: SessionService,
        memory: MemoryPlugin,
        search: SearchService,
        provider_service: ProviderService,
    ) -> None:
        self.config = config
        self.providers = provider_service
        self.db = db
        self.gateway = gateway
        self.prompts = prompts
        self.sessions = sessions
        self.memory = memory
        self.search = search
        self.context = ContextBuilder(config)
        self._background: set[asyncio.Task[Any]] = set()

    async def close(self) -> None:
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        await self.gateway.close()
        await self.memory.close()
        await self.search.close()

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain_background(self) -> None:
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    async def prepare(self, event: InboundEvent) -> PreparedTurn:
        try:
            parts = event.normalized_parts()
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        metadata = event.metadata or {}
        requested_session_id = str(event.session_id) if event.session_id else None
        legacy_session_id = False
        if not requested_session_id:
            legacy_value = metadata.get("session_id")
            if isinstance(legacy_value, str) and legacy_value.strip():
                try:
                    requested_session_id = str(uuid.UUID(legacy_value.strip()))
                    legacy_session_id = True
                except ValueError:
                    log.warning(
                        "metadata_session_id_invalid event_id=%s", str(event.event_id)
                    )
        normalized = NormalizedInput(
            event_id=str(event.event_id),
            session_id=requested_session_id,
            parts=parts,
            metadata=metadata,
            legacy_session_id=legacy_session_id,
        )
        if legacy_session_id:
            log.warning(
                "metadata_session_id_used event_id=%s; session_id belongs at the event top level",
                normalized.event_id,
            )

        replay = await self.db.get_message_by_source_event(normalized.event_id)
        if replay:
            session = await self.sessions.get(replay.session_id)
            return self._replay_turn(normalized, session, replay)

        text = normalized.text.strip()
        # Resolve the user-configured provider defaults *before* creating the
        # session. Passing only model=None let ensure() persist the static
        # gateway.default_model, which then shadowed the settings default in
        # _select_model (Dounai audit, 2026-11-05).
        defaults = await self.providers.defaults()
        default_ref = self.providers.canonical_ref(
            defaults.provider_kind, defaults.provider_id, defaults.model
        )
        session, created = await self.sessions.ensure(
            normalized.session_id,
            title_hint=text[:60] if text else "Image conversation",
            model=default_ref,
            prompt_profile=None,
            provider_kind=defaults.provider_kind,
            provider_id=defaults.provider_id,
            model_name=defaults.model,
        )
        normalized.session_created = created
        if created and not normalized.session_id:
            log.warning(
                "session_id_missing event_id=%s; new session created id=%s",
                normalized.event_id,
                session.id,
            )
        elif created:
            log.info("session_created event_id=%s session_id=%s", normalized.event_id, session.id)

        kind, provider_id, model_name, explicit = self._select_model(metadata, session, defaults)
        requested_model = self.providers.canonical_ref(kind, provider_id, model_name)
        if not created and not explicit and requested_model == session.model:
            available, reason = await self.providers.session_availability(session)
            if not available:
                raise OmniError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "当前会话使用的提供方不可用，请重新选择提供方和模型。",
                    status_code=409,
                    details={"reason": reason},
                )
        if kind == "custom":
            snapshot = self.providers.get_custom(provider_id)
            if not snapshot or not snapshot.provider.enabled:
                raise OmniError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "提供方已禁用或删除，请在聊天页重新选择。",
                    status_code=409,
                    details={"providerId": provider_id},
                )
            if not any(model.name == model_name for model in snapshot.provider.models):
                raise OmniError(
                    ErrorCode.MODEL_UNAVAILABLE,
                    "模型已删除，请重新选择模型。",
                    status_code=409,
                    details={"model": model_name},
                )
        elif not self.config.model_config_by_id(requested_model):
            raise OmniError(
                ErrorCode.UNKNOWN_MODEL,
                "Unknown model id.",
                status_code=404,
                details={"model": requested_model},
            )

        if (
            created
            or explicit
            or session.provider_kind != kind
            or session.provider_id != provider_id
            or session.model_name != model_name
        ):
            session = await self.sessions.update(
                session.id,
                {
                    "model": requested_model,
                    "provider_kind": kind,
                    "provider_id": provider_id,
                    "model_name": model_name,
                },
            )

        explicit_profile = metadata.get("prompt_profile")
        profile_id = str(
            explicit_profile or session.prompt_profile or self.config.prompts.default_profile
        )
        if explicit_profile:
            await self.prompts.get_profile(profile_id)  # 404 if unknown

        web_search_requested = _as_bool(metadata.get("web_search", session.web_search))

        plan = self.gateway.plan(requested_model, parts)
        output_event_id = str(uuid.uuid4())

        user_message = StoredMessage.new(
            session_id=session.id,
            role="user",
            content=text,
            parts=parts,
            metadata={
                "input_event_id": normalized.event_id,
                "modalities": sorted(normalized.modalities),
                "locale": str(metadata.get("locale") or "")[:32],
            },
            token_estimate=estimate_tokens(text)
            + sum(850 for part in parts if part.modality == "image"),
        )
        await self.db.add_message(user_message)
        if text and session.title in {"New conversation", "Image conversation"}:
            session = await self.sessions.update(session.id, {"title": text[:60]})
        history = await self.db.list_messages(
            session.id, limit=self.config.gateway.max_history_messages
        )
        if plan.degraded:
            history = [
                message.model_copy(update={"parts": plan.effective_parts})
                if message.id == user_message.id
                else message
                for message in history
            ]

        memory_result = await self._query_memory(text, session.id)
        search_outcome = await self._query_search(text, web_search_requested)

        variables = self.prompts.safe_variables(
            {
                "current_time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "locale": str(metadata.get("locale") or "zh-CN")[:32],
                "model": requested_model,
                "session_id": session.id,
                "user_input": text[:4000],
            }
        )
        template_prompt, effective_profile = await self.prompts.get_active_template(
            profile_id, variables
        )
        memory_result.items = self._budget_memory_items(memory_result.items)
        system_prompt = compose_system_prompt(
            template_prompt=template_prompt,
            search_results=search_outcome.results,
            memory_items=memory_result.items,
        )

        max_tokens = min(plan.primary.max_output_tokens, self.config.gateway.max_output_tokens)
        budget = context_budget(self.config, plan.primary.context_window, max_tokens)
        built = self.context.build(
            system_prompt=system_prompt,
            history=history,
            budget_tokens=budget,
        )
        return PreparedTurn(
            input_event_id=normalized.event_id,
            output_event_id=output_event_id,
            session=session,
            user_text=text,
            plan=plan,
            messages=built.messages,
            system_prompt=system_prompt,
            prompt_profile=effective_profile,
            web_search_requested=web_search_requested,
            search_outcome=search_outcome,
            memory_result=memory_result,
            max_tokens=max_tokens,
            temperature=self.config.gateway.temperature,
            top_p=self.config.gateway.top_p,
            dropped_messages=built.dropped_messages,
            session_created=created,
            legacy_session_id=normalized.legacy_session_id,
        )

    def _select_model(
        self,
        metadata: dict[str, Any],
        session: SessionRecord,
        defaults: Any,
    ) -> tuple[str, str, str, bool]:
        explicit_kind = metadata.get("providerKind")
        explicit_provider = metadata.get("providerId")
        explicit_model = metadata.get("modelName") or metadata.get("model")
        if explicit_kind or explicit_provider:
            kind = str(explicit_kind or "custom")
            if kind not in {"builtin", "custom"}:
                raise InvalidInputError("不支持的提供方类型。")
            if not explicit_provider or not explicit_model:
                raise InvalidInputError("必须同时提供 providerId 和 model。")
            return kind, str(explicit_provider), str(explicit_model), True
        if metadata.get("model"):
            kind, provider_id, model_name = self.providers.resolve_ref(str(metadata["model"]))
            return kind, provider_id, model_name, True
        if session.provider_kind and session.provider_id and session.model_name:
            return session.provider_kind, session.provider_id, session.model_name, False
        if session.model:
            kind, provider_id, model_name = self.providers.resolve_ref(session.model)
            return kind, provider_id, model_name, False
        return (
            defaults.provider_kind,
            defaults.provider_id,
            defaults.model,
            False,
        )

    def _replay_turn(
        self, normalized: NormalizedInput, session: SessionRecord, message: StoredMessage
    ) -> PreparedTurn:
        metadata = message.metadata
        tags = metadata.get("tags")
        if not isinstance(tags, dict):
            tags = classify(
                message.content,
                used_search=bool(metadata.get("search_used")),
                used_memory=bool(metadata.get("memory_used")),
            ).tags.model_dump()
        output = OutputEvent(
            event_id=str(metadata.get("output_event_id") or message.id),
            session_id=session.id,
            content=message.content,
            stream=False,
            tags=OutputTags.model_validate(tags),
        )
        plan = RoutePlan(
            requested_model=str(metadata.get("requested_model") or session.model or ""),
            primary=self.config.model_config_by_id(session.model or "") or self.config.models[0],
            candidates=[],
            effective_parts=[],
        )
        return PreparedTurn(
            input_event_id=normalized.event_id,
            output_event_id=output.event_id,
            session=session,
            user_text=normalized.text,
            plan=plan,
            messages=[],
            system_prompt="",
            prompt_profile=session.prompt_profile or self.config.prompts.default_profile,
            web_search_requested=False,
            search_outcome=SearchOutcome(available=False, reason="replay"),
            memory_result=MemoryQueryResult(available=False, reason="replay"),
            max_tokens=0,
            temperature=0,
            top_p=0,
            replay=message,
        )

    async def _query_memory(self, text: str, session_id: str) -> MemoryQueryResult:
        if not text:
            return MemoryQueryResult(available=False, reason="empty_query")
        try:
            timeout = max(1.0, self.config.memory.query_timeout_ms / 1000 + 0.2)
            async with asyncio.timeout(timeout):
                return await self.memory.query(text, session_id=session_id)
        except TimeoutError:
            log.warning("memory_query_timeout")
            return MemoryQueryResult(available=False, reason="timeout")
        except Exception as exc:  # reserved plugin must never break chat
            log.warning("memory_query_failed type=%s", type(exc).__name__)
            return MemoryQueryResult(available=False, reason="error")

    async def _query_search(self, text: str, requested: bool) -> SearchOutcome:
        if not requested or not text or not self.search.available:
            return SearchOutcome(
                available=False,
                reason="disabled" if not requested else "unavailable",
                provider=self.config.search.provider,
            )
        try:
            timeout = self.config.search.timeout_s + 2
            async with asyncio.timeout(timeout):
                return await self.search.search(text)
        except TimeoutError:
            log.warning("search_timeout")
            return SearchOutcome(
                available=False, reason="timeout", provider=self.config.search.provider
            )
        except Exception as exc:
            log.warning("search_failed type=%s", type(exc).__name__)
            return SearchOutcome(
                available=False, reason="error", provider=self.config.search.provider
            )

    def _budget_memory_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Reserved plugin packaging: cap injected memory by a token budget."""
        max_tokens = max(0, self.config.memory.max_context_tokens)
        selected: list[dict[str, Any]] = []
        used = 0
        for item in items[: self.config.memory.max_items]:
            content = str(item.get("content", ""))
            cost = estimate_tokens(content) + 8
            if selected and used + cost > max_tokens:
                break
            if not selected and cost > max_tokens:
                continue
            selected.append(item)
            used += cost
        return selected

    def _query_citations(self, prepared: PreparedTurn) -> list[Citation]:
        return prepared.search_outcome.citations if prepared.search_outcome.results else []

    def _tags(self, text: str, prepared: PreparedTurn) -> OutputTags:
        return classify(
            text,
            used_search=bool(prepared.search_outcome.results),
            used_memory=bool(prepared.memory_result.items),
            fallback_used=prepared.plan.fallback_used,
        ).tags

    def _meta(self, prepared: PreparedTurn) -> dict[str, Any]:
        return {
            "event_id": prepared.output_event_id,
            "session_id": prepared.session.id,
            "requested_model": prepared.plan.requested_model,
            "planned_model": prepared.plan.primary.id,
            "prompt_profile": prepared.prompt_profile,
            "web_search_requested": prepared.web_search_requested,
            "search": {
                "available": prepared.search_outcome.available,
                "reason": prepared.search_outcome.reason,
                "results": len(prepared.search_outcome.results),
                "provider": prepared.search_outcome.provider,
            },
            "memory": {
                "available": prepared.memory_result.available,
                "reason": prepared.memory_result.reason,
                "items": len(prepared.memory_result.items),
                "reserved": True,
            },
            "capability_reason": prepared.plan.capability_reason,
            "degraded": prepared.plan.degraded,
            "dropped_messages": prepared.dropped_messages,
            "session_created": prepared.session_created,
            "legacy_session_id": prepared.legacy_session_id,
            "tag_contract": tag_contract(),
        }

    async def run_json(self, event: InboundEvent) -> TurnResult:
        prepared = await self.prepare(event)
        if prepared.replay:
            return await self._replay_result(prepared)
        final: OutputEvent | None = None
        citations: list[Citation] = []
        error: dict[str, Any] | None = None
        async for frame in self.stream_prepared(prepared):
            if frame.event == "citations":
                citations = [
                    Citation(**item)
                    for item in frame.data.get("citations", [])  # type: ignore[arg-type]
                ]
            elif frame.event == "final":
                final = OutputEvent.model_validate(frame.data)
            elif frame.event == "error":
                error = frame.data
        if final is None:
            if error:
                raise _error_from_frame(error)
            raise OmniError(
                ErrorCode.INTERNAL_ERROR,
                "The model produced no output.",
                status_code=500,
            )
        return TurnResult(
            output=final,
            citations=citations,
            meta=self._meta(prepared),
            usage=TokenUsage(
                completion_tokens=estimate_tokens(final.content),
                total_tokens=estimate_tokens(final.content),
            ),
        )

    async def _replay_result(self, prepared: PreparedTurn) -> TurnResult:
        message = prepared.replay
        assert message is not None
        content = message.content
        metadata = message.metadata
        tags = metadata.get("tags")
        if not isinstance(tags, dict):
            tags = classify(
                content,
                used_search=bool(metadata.get("search_used")),
                used_memory=bool(metadata.get("memory_used")),
            ).tags.model_dump()
        output = OutputEvent(
            event_id=prepared.output_event_id,
            session_id=prepared.session.id,
            content=content,
            stream=False,
            tags=OutputTags.model_validate(tags),
        )
        raw_citations = await self.db.get_citations(output.event_id)
        citations = [
            Citation(
                url=item["url"],
                title=item["title"],
                snippet=item["snippet"],
                rank=item["rank"],
                source=item["source"],
            )
            for item in raw_citations
        ]
        return TurnResult(
            output=output,
            citations=citations,
            meta={**self._meta(prepared), "replay": True},
            replay=True,
        )

    async def stream_prepared(self, prepared: PreparedTurn) -> Any:
        if prepared.replay is not None:
            result = await self._replay_result(prepared)
            yield StreamFrame(event="final", data=result.output.model_dump())
            return

        yield StreamFrame(event="meta", data=self._meta(prepared))
        content_parts: list[str] = []
        usage = TokenUsage()
        route_sent = False
        interrupted = False
        started = time.monotonic()
        try:
            async for delta in self.gateway.stream(
                plan=prepared.plan,
                messages=prepared.messages,
                max_tokens=prepared.max_tokens,
                temperature=prepared.temperature,
                top_p=prepared.top_p,
            ):
                if delta.usage:
                    usage = _merge_usage(usage, delta.usage)
                if not delta.text:
                    continue
                if not route_sent:
                    route_sent = True
                    yield StreamFrame(
                        event="route",
                        data={
                            "provider": prepared.plan.selected_provider,
                            "model": prepared.plan.selected_model,
                            "fallback": prepared.plan.fallback_used,
                        },
                    )
                content_parts.append(delta.text)
                text = "".join(content_parts)
                tags = self._tags(text, prepared)
                yield StreamFrame(
                    event="delta",
                    data=OutputEvent(
                        event_id=prepared.output_event_id,
                        session_id=prepared.session.id,
                        content=delta.text,
                        stream=True,
                        tags=tags,
                    ).model_dump(),
                )
        except GenerationInterruptedError:
            interrupted = True
            log.warning("generation_interrupted event_id=%s", prepared.output_event_id)
        except (ProviderError, OmniError) as exc:
            if not content_parts:
                await self._persist_failure(prepared, exc)
                yield StreamFrame(
                    event="error",
                    data=exc.as_dict(),
                )
                return
            interrupted = True
            log.warning("generation_error_after_partial code=%s", exc.code.value)
        except asyncio.CancelledError:
            if content_parts and self.config.storage.keep_partial_on_disconnect:
                partial = "".join(content_parts)
                await asyncio.shield(
                    self._persist_success(
                        prepared,
                        content=partial,
                        usage=usage,
                        interrupted=True,
                        citations=[],
                        tags=self._tags(partial, prepared),
                    )
                )
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            log.error("generation_unexpected type=%s", type(exc).__name__)
            if not content_parts:
                error = OmniError(
                    ErrorCode.INTERNAL_ERROR,
                    "The model request failed unexpectedly.",
                    status_code=500,
                )
                await self._persist_failure(prepared, error)
                yield StreamFrame(event="error", data=error.as_dict())
                return
            interrupted = True

        text = "".join(content_parts)
        if not text:
            error = OmniError(
                ErrorCode.NO_PROVIDER_AVAILABLE,
                "No model produced output. Check provider configuration.",
                status_code=503,
            )
            await self._persist_failure(prepared, error)
            yield StreamFrame(event="error", data=error.as_dict())
            return

        citations = self._query_citations(prepared)
        final_content = (
            append_citations(text, citations)
            if self.config.gateway.citations_in_content
            else text
        )
        # Classify the answer itself; any source appendix must not turn a prose
        # answer into "list" or a code answer into "mixed".
        answer_tags = self._tags(text, prepared)
        output = OutputEvent(
            event_id=prepared.output_event_id,
            session_id=prepared.session.id,
            content=final_content,
            stream=False,
            tags=answer_tags,
        )
        await self._persist_success(
            prepared,
            content=final_content,
            usage=usage,
            interrupted=interrupted,
            citations=citations,
            tags=answer_tags,
        )
        if prepared.search_outcome.results:
            yield StreamFrame(
                event="citations",
                data={
                    "citations": [
                        {
                            "url": c.url,
                            "title": c.title,
                            "snippet": c.snippet,
                            "rank": c.rank,
                            "source": c.source,
                        }
                        for c in citations
                    ]
                },
            )
        if interrupted:
            yield StreamFrame(
                event="warning",
                data={
                    "code": ErrorCode.GENERATION_INTERRUPTED.value,
                    "message": "The stream was interrupted; the partial answer was archived.",
                },
            )
        yield StreamFrame(event="final", data=output.model_dump())
        log.info(
            "turn_completed event_id=%s model=%s fallback=%s duration_ms=%s chars=%s",
            prepared.output_event_id,
            prepared.plan.selected_model or prepared.plan.primary.id,
            prepared.plan.fallback_used,
            int((time.monotonic() - started) * 1000),
            len(final_content),
            extra={
                "event_id": prepared.output_event_id,
                "session_id": prepared.session.id,
                "model": prepared.plan.selected_model,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )

    async def _persist_success(
        self,
        prepared: PreparedTurn,
        *,
        content: str,
        usage: TokenUsage,
        interrupted: bool,
        citations: list[Citation],
        tags: OutputTags | None = None,
    ) -> None:
        tags = tags or self._tags(content, prepared)
        metadata = {
            "output_event_id": prepared.output_event_id,
            "provider": prepared.plan.selected_provider,
            "model": prepared.plan.selected_model,
            "requested_model": prepared.plan.requested_model,
            "fallback_used": prepared.plan.fallback_used,
            "capability_reason": prepared.plan.capability_reason,
            "degraded": prepared.plan.degraded,
            "search_used": bool(prepared.search_outcome.results),
            "memory_used": bool(prepared.memory_result.items),
            "interrupted": interrupted,
            "tags": tags.model_dump(),
            "usage": usage.as_dict(),
        }
        message = StoredMessage.new(
            session_id=prepared.session.id,
            role="assistant",
            content=content,
            metadata=metadata,
            token_estimate=estimate_tokens(content),
        )
        try:
            await self.db.add_message(message, source_event_id=prepared.input_event_id)
        except Exception:
            # A duplicate delivery may already have persisted the same event.
            existing = await self.db.get_message_by_source_event(prepared.input_event_id)
            if existing:
                return
            raise
        await self.db.add_citations(prepared.session.id, prepared.output_event_id, citations)
        await self.db.update_session(prepared.session.id, {"updated_at": iso_now()})
        self._schedule_writeback(prepared, content, tags)

    async def _persist_failure(self, prepared: PreparedTurn, error: OmniError) -> None:
        log.warning("turn_failed code=%s", error.code.value)
        await self.db.update_session(prepared.session.id, {"updated_at": iso_now()})
        self._schedule_writeback(prepared, "", None)

    def _schedule_writeback(
        self, prepared: PreparedTurn, content: str, tags: OutputTags | None
    ) -> None:
        if not getattr(self.memory, "configured", False):
            return
        if not self.config.memory.async_writeback:
            return
        items = [
            {
                "role": "user",
                "content": prepared.user_text,
                "created_at": prepared.session.created_at,
            }
        ]
        if content:
            items.append(
                {
                    "role": "assistant",
                    "content": content,
                    "created_at": iso_now(),
                    "metadata": {"tags": tags.model_dump() if tags else {}},
                }
            )
        task = asyncio.create_task(
            self.memory.writeback(
                session_id=prepared.session.id,
                event_id=prepared.output_event_id,
                items=items,
            )
        )
        self._track(task)


def _merge_usage(current: TokenUsage, new: TokenUsage) -> TokenUsage:
    prompt = max(current.prompt_tokens, new.prompt_tokens)
    completion = max(current.completion_tokens, new.completion_tokens)
    total = max(current.total_tokens, new.total_tokens, prompt + completion)
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _error_from_frame(data: dict[str, Any]) -> OmniError:
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return OmniError(ErrorCode.INTERNAL_ERROR, "Model request failed.", status_code=500)
    code_value = str(error.get("code") or "INTERNAL_ERROR")
    try:
        code = ErrorCode(code_value)
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    details = error.get("details")
    return OmniError(
        code,
        str(error.get("message") or "Model request failed."),
        status_code=503
        if code
        in {
            ErrorCode.ALL_PROVIDERS_FAILED,
            ErrorCode.NO_PROVIDER_AVAILABLE,
            ErrorCode.PROVIDER_RATE_LIMITED,
        }
        else 502,
        details=details if isinstance(details, dict) else None,
    )
