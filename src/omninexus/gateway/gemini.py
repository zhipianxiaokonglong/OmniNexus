"""Google Gemini generateContent streaming adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx

from omninexus.core.models import ChatDelta, LLMMessage, TokenUsage
from omninexus.errors import ErrorCode, ProviderError
from omninexus.gateway.base import BaseProvider, map_http_error, parse_image_url, parse_sse_json


def _parts_for(message: LLMMessage) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if message.content:
        parts.append({"text": message.content})
    for image in message.images:
        payload = parse_image_url(image.url, image.mime_type)
        if payload and payload.is_base64:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": payload.mime_type,
                        "data": payload.data,
                    }
                }
            )
        else:
            parts.append(
                {
                    "text": (
                        f"[Image attachment not inlined. alt={image.alt or 'none'} "
                        f"url={image.url[:500]}]"
                    )
                }
            )
    if not parts:
        parts.append({"text": "(empty)"})
    return parts


class GeminiProvider(BaseProvider):
    type_name = "gemini"

    async def stream(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        max_tokens: int,
        temperature: float,
        top_p: float,
    ) -> AsyncIterator[ChatDelta]:
        key = self.api_key
        if not key:
            raise ProviderError(
                "Provider API key is not configured.",
                code=ErrorCode.PROVIDER_AUTH,
                status_code=503,
                retryable=False,
                provider=self.name,
                model=model,
            )
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                system_parts.append(message.content)
                continue
            role = "model" if message.role == "assistant" else "user"
            contents.append({"role": role, "parts": _parts_for(message)})
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
                "topP": top_p,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        headers = self.headers(
            {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "x-goog-api-key": key,
            }
        )
        url = (
            f"{self.config.base_url}/v1beta/models/{quote(model, safe='')}"
            ":streamGenerateContent?alt=sse"
        )
        try:
            async with self.client().stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread())[:2048].decode("utf-8", "ignore")
                    raise await map_http_error(resp, provider=self.name, model=model, body=body)
                async for line in resp.aiter_lines():
                    data = parse_sse_json(line)
                    if not data:
                        continue
                    if data.get("error"):
                        raise ProviderError(
                            "Provider returned an error during streaming.",
                            code=ErrorCode.PROVIDER_ERROR,
                            status_code=502,
                            retryable=False,
                            provider=self.name,
                            model=model,
                        )
                    feedback = data.get("promptFeedback") or {}
                    if feedback.get("blockReason"):
                        raise ProviderError(
                            "Provider blocked the prompt.",
                            code=ErrorCode.PROVIDER_ERROR,
                            status_code=422,
                            retryable=False,
                            provider=self.name,
                            model=model,
                        )
                    usage_meta = data.get("usageMetadata") or {}
                    usage = None
                    if usage_meta:
                        prompt_tokens = int(usage_meta.get("promptTokenCount") or 0)
                        completion_tokens = int(usage_meta.get("candidatesTokenCount") or 0)
                        usage = TokenUsage(
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            total_tokens=int(
                                usage_meta.get("totalTokenCount")
                                or prompt_tokens + completion_tokens
                            ),
                        )
                    candidates = data.get("candidates") or []
                    if not candidates:
                        if usage:
                            yield ChatDelta(text="", usage=usage)
                        continue
                    candidate = candidates[0] or {}
                    texts: list[str] = []
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        if isinstance(part, dict) and not part.get("thought"):
                            text = part.get("text")
                            if isinstance(text, str):
                                texts.append(text)
                    finish = candidate.get("finishReason")
                    if texts or usage or finish:
                        yield ChatDelta(text="".join(texts), usage=usage, finish_reason=finish)
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "Provider request timed out.",
                code=ErrorCode.PROVIDER_TIMEOUT,
                status_code=504,
                retryable=True,
                provider=self.name,
                model=model,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "Could not reach provider.",
                code=ErrorCode.PROVIDER_ERROR,
                status_code=503,
                retryable=True,
                provider=self.name,
                model=model,
            ) from exc
