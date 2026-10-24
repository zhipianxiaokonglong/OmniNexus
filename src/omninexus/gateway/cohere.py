"""Cohere Chat v2 adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from omninexus.core.models import ChatDelta, LLMMessage, TokenUsage
from omninexus.errors import ErrorCode, ProviderError
from omninexus.gateway.base import BaseProvider, map_http_error, parse_sse_json


class CohereProvider(BaseProvider):
    type_name = "cohere"

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
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message.role if message.role in ("system", "user", "assistant") else "user"
            if message.images and role == "user":
                content: list[dict[str, Any]] = []
                if message.content:
                    content.append({"type": "text", "text": message.content})
                for image in message.images:
                    content.append({"type": "image_url", "image_url": {"url": image.url}})
                converted.append({"role": role, "content": content})
            else:
                converted.append({"role": role, "content": message.content})
        payload: dict[str, Any] = {
            "model": model,
            "messages": converted,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "p": top_p,
            "stream": True,
        }
        headers = self.headers(
            {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {key}",
            }
        )
        url = f"{self.config.base_url}/v2/chat"
        try:
            async with self.client().stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread())[:2048].decode("utf-8", "ignore")
                    raise await map_http_error(resp, provider=self.name, model=model, body=body)
                async for line in resp.aiter_lines():
                    data = parse_sse_json(line)
                    if not data:
                        continue
                    event_type = data.get("type")
                    if event_type == "content-delta":
                        delta = data.get("delta") or {}
                        message = delta.get("message") or {}
                        content = message.get("content") or {}
                        text = content.get("text")
                        if isinstance(text, str) and text:
                            yield ChatDelta(text=text)
                    elif event_type == "message-end":
                        usage = (data.get("delta") or {}).get("usage") or {}
                        tokens = usage.get("tokens") or usage
                        input_tokens = int(tokens.get("input_tokens") or 0)
                        output_tokens = int(tokens.get("output_tokens") or 0)
                        yield ChatDelta(
                            text="",
                            usage=TokenUsage(
                                prompt_tokens=input_tokens,
                                completion_tokens=output_tokens,
                                total_tokens=input_tokens + output_tokens,
                            ),
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
