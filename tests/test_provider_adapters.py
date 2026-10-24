"""Native Anthropic, Gemini and Cohere adapter contract tests."""

from __future__ import annotations

import json

import httpx
import pytest

from omninexus.config import GatewayConfig, ProviderConfig
from omninexus.core.models import LLMImage, LLMMessage
from omninexus.errors import ProviderError
from omninexus.gateway.anthropic import AnthropicProvider
from omninexus.gateway.cohere import CohereProvider
from omninexus.gateway.gemini import GeminiProvider
from tests.conftest import make_transport


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _data(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def anthropic_sse() -> httpx.Response:
    body = "".join(
        [
            _sse(
                "message_start",
                {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            ),
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
            ),
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": " Claude"},
                },
            ),
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 2},
                },
            ),
            _sse("message_stop", {"type": "message_stop"}),
        ]
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


@pytest.mark.asyncio
async def test_anthropic_native_stream_and_image() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return anthropic_sse()

    provider = AnthropicProvider(
        "anthropic",
        ProviderConfig(
            type="anthropic",
            base_url="https://anth.test",
            api_key_env="ANTHROPIC_KEY",
        ),
        gateway=GatewayConfig(),
        transport=make_transport(handler),
    )
    try:
        chunks = [
            delta.text
            async for delta in provider.stream(
                model="claude-test",
                messages=[
                    LLMMessage(role="system", content="system rule"),
                    LLMMessage(
                        role="user",
                        content="look",
                        images=[LLMImage(url="data:image/png;base64,AAAA", mime_type="image/png")],
                    ),
                ],
                max_tokens=64,
                temperature=0.2,
                top_p=0.9,
            )
        ]
        assert "".join(chunks) == "Hello Claude"
        body = json.loads(seen[0].read())
        assert body["system"] == "system rule"
        assert body["messages"][0]["content"][1]["source"]["type"] == "base64"
        assert seen[0].headers["anthropic-version"] == "2023-06-01"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_gemini_native_stream_and_image() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = (
            'data: {"candidates":[{"content":{"parts":[{"text":"Hello"}]}}]}\n\n'
            'data: {"candidates":[{"content":{"parts":[{"text":" Gemini"}]},'
            '"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":4,'
            '"candidatesTokenCount":2,"totalTokenCount":6}}\n\n'
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    provider = GeminiProvider(
        "gemini",
        ProviderConfig(
            type="gemini",
            base_url="https://gem.test",
            api_key_env="GEMINI_KEY",
        ),
        gateway=GatewayConfig(),
        transport=make_transport(handler),
    )
    try:
        chunks = [
            delta.text
            async for delta in provider.stream(
                model="gemini-test",
                messages=[
                    LLMMessage(role="system", content="be brief"),
                    LLMMessage(
                        role="user",
                        content="look",
                        images=[LLMImage(url="data:image/png;base64,BBBB", mime_type="image/png")],
                    ),
                ],
                max_tokens=64,
                temperature=0.2,
                top_p=0.9,
            )
        ]
        assert "".join(chunks) == "Hello Gemini"
        body = json.loads(seen[0].read())
        assert body["systemInstruction"]["parts"][0]["text"] == "be brief"
        assert body["contents"][0]["parts"][1]["inline_data"]["data"] == "BBBB"
        assert "key=" not in str(seen[0].url)
        assert seen[0].headers["x-goog-api-key"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_cohere_native_stream() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = "".join(
            [
                _data(
                    {
                        "type": "content-delta",
                        "delta": {"message": {"content": {"text": "Hi"}}},
                    }
                ),
                _data(
                    {
                        "type": "content-delta",
                        "delta": {"message": {"content": {"text": " Cohere"}}},
                    }
                ),
                _data(
                    {
                        "type": "message-end",
                        "delta": {"usage": {"tokens": {"input_tokens": 3, "output_tokens": 2}}},
                    }
                ),
            ]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    provider = CohereProvider(
        "cohere",
        ProviderConfig(
            type="cohere",
            base_url="https://cohere.test",
            api_key_env="COHERE_KEY",
        ),
        gateway=GatewayConfig(),
        transport=make_transport(handler),
    )
    try:
        chunks = [
            delta.text
            async for delta in provider.stream(
                model="command-test",
                messages=[LLMMessage(role="user", content="hello")],
                max_tokens=64,
                temperature=0.2,
                top_p=0.9,
            )
        ]
        assert "".join(chunks) == "Hi Cohere"
        assert json.loads(seen[0].read())["messages"][0]["role"] == "user"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_provider_auth_error_is_not_retryable() -> None:
    provider = AnthropicProvider(
        "anthropic",
        ProviderConfig(
            type="anthropic",
            base_url="https://anth.test",
            api_key_env="ANTHROPIC_KEY",
        ),
        gateway=GatewayConfig(),
        transport=make_transport(lambda request: httpx.Response(401, json={"error": "bad"})),
    )
    try:
        with pytest.raises(ProviderError) as exc:
            async for _ in provider.stream(
                model="claude-test",
                messages=[LLMMessage(role="user", content="hello")],
                max_tokens=10,
                temperature=0.2,
                top_p=1.0,
            ):
                pass
        assert exc.value.retryable is False
        assert exc.value.code.value == "PROVIDER_AUTH"
    finally:
        await provider.close()
