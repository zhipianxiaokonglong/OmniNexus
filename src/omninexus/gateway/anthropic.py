"""Anthropic Messages API adapter (native SSE)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from omninexus.core.models import ChatDelta, LLMMessage, TokenUsage
from omninexus.errors import ErrorCode, ProviderError
from omninexus.gateway.base import BaseProvider, map_http_error, parse_image_url, parse_sse_json


def _content_blocks(message: LLMMessage) -> str | list[dict[str, Any]]:
    if not message.images:
        return message.content or "(empty)"
    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    for image in message.images:
        payload = parse_image_url(image.url, image.mime_type)
        if payload and payload.is_base64:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": payload.mime_type,
                        "data": payload.data,
                    },
                }
            )
        elif image.url.startswith(("https://", "http://")):
            blocks.append({"type": "image", "source": {"type": "url", "url": image.url}})
        else:
            blocks.append(
                {
                    "type": "text",
                    "text": f"[Image attachment unavailable: {image.alt or 'no alt text'}]",
                }
            )
    return blocks or "(empty)"


class AnthropicProvider(BaseProvider):
    type_name = "anthropic"

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
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                system_parts.append(message.content)
                continue
            role = "assistant" if message.role == "assistant" else "user"
            converted.append({"role": role, "content": _content_blocks(message)})
        payload: dict[str, Any] = {
            "model": model,
            "messages": converted,
            "max_tokens": max_tokens,
            "temperature": min(max(temperature, 0.0), 1.0),
            "top_p": top_p,
            "stream": True,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        headers = self.headers(
            {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            }
        )
        url = f"{self.config.base_url}/v1/messages"
        try:
            async with self.client().stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread())[:2048].decode("utf-8", "ignore")
                    raise await map_http_error(resp, provider=self.name, model=model, body=body)
                prompt_tokens = 0
                completion_tokens = 0
                async for line in resp.aiter_lines():
                    data = parse_sse_json(line)
                    if not data:
                        continue
                    event_type = data.get("type")
                    if event_type == "message_start":
                        usage = (data.get("message") or {}).get("usage") or {}
                        prompt_tokens = int(usage.get("input_tokens") or 0)
                    elif event_type == "content_block_delta":
                        delta = data.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            yield ChatDelta(text=str(delta.get("text") or ""))
                    elif event_type == "message_delta":
                        usage = data.get("usage") or {}
                        completion_tokens = int(usage.get("output_tokens") or 0)
                        yield ChatDelta(
                            text="",
                            usage=TokenUsage(
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                total_tokens=prompt_tokens + completion_tokens,
                            ),
                            finish_reason=(data.get("delta") or {}).get("stop_reason"),
                        )
                    elif event_type == "error":
                        raise ProviderError(
                            "Provider returned an error during streaming.",
                            code=ErrorCode.PROVIDER_ERROR,
                            status_code=502,
                            retryable=False,
                            provider=self.name,
                            model=model,
                        )
                    elif event_type == "message_stop":
                        return
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
