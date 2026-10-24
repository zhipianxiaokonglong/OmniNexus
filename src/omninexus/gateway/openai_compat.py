"""OpenAI-compatible chat-completions adapter.

Works with OpenAI, DeepSeek, Mistral, xAI, Qwen/DashScope, Doubao/Ark, GLM,
Moonshot, Qianfan, Spark, Hunyuan, MiniMax, OpenRouter and local OpenAI-compatible
servers (Ollama, LM Studio, vLLM, llama.cpp).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from omninexus.core.models import ChatDelta, LLMMessage, TokenUsage
from omninexus.errors import ErrorCode, ProviderError
from omninexus.gateway.base import BaseProvider, map_http_error, parse_sse_json


class OpenAICompatibleProvider(BaseProvider):
    type_name = "openai"

    def _payload(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        max_tokens: int,
        temperature: float,
        top_p: float,
    ) -> dict[str, Any]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "user" and message.images:
                content: list[dict[str, Any]] = []
                if message.content:
                    content.append({"type": "text", "text": message.content})
                for image in message.images:
                    content.append({"type": "image_url", "image_url": {"url": image.url}})
                converted.append({"role": "user", "content": content})
            else:
                converted.append({"role": message.role, "content": message.content})

        payload: dict[str, Any] = {
            "model": model,
            "messages": converted,
            "stream": True,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
        }
        options = self.request_options()
        # Provider-specific switches; never allow overriding messages/model.
        for key in ("stream_options", "frequency_penalty", "presence_penalty", "stop"):
            if key in options:
                payload[key] = options[key]
        if options.get("omit_temperature"):
            payload.pop("temperature", None)
        if options.get("max_tokens_field") == "max_completion_tokens":
            payload["max_completion_tokens"] = payload.pop("max_tokens")
        return payload

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
        if self.config.requires_key and not key:
            raise ProviderError(
                "Provider API key is not configured.",
                code=ErrorCode.PROVIDER_AUTH,
                status_code=503,
                retryable=False,
                provider=self.name,
                model=model,
            )
        headers = self.headers(
            {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
        )
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = f"{self.config.base_url}/chat/completions"
        payload = self._payload(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        )
        try:
            async with self.client().stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread())[:2048].decode("utf-8", "ignore")
                    raise await map_http_error(resp, provider=self.name, model=model, body=body)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    if line.strip() == "data: [DONE]":
                        return
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
                    usage = data.get("usage")
                    parsed_usage = None
                    if isinstance(usage, dict):
                        parsed_usage = TokenUsage(
                            prompt_tokens=int(usage.get("prompt_tokens") or 0),
                            completion_tokens=int(usage.get("completion_tokens") or 0),
                            total_tokens=int(usage.get("total_tokens") or 0),
                        )
                    choices = data.get("choices") or []
                    if not choices:
                        if parsed_usage:
                            yield ChatDelta(text="", usage=parsed_usage)
                        continue
                    choice = choices[0] or {}
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if isinstance(text, list):
                        text = "".join(
                            item.get("text", "")
                            for item in text
                            if isinstance(item, dict) and item.get("type") == "text"
                        )
                    if not isinstance(text, str):
                        text = ""
                    yield ChatDelta(
                        text=text,
                        usage=parsed_usage,
                        finish_reason=choice.get("finish_reason"),
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
