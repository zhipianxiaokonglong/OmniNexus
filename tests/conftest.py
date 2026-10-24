"""Shared test fixtures and a deterministic OpenAI-compatible mock provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from omninexus.app import create_app
from omninexus.config import (
    AppConfig,
    MemoryConfig,
    ModelConfig,
    ProviderConfig,
    SearchConfig,
    load_config,
)


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALPHA_KEY",
        "BETA_KEY",
        "VISION_KEY",
        "SEARCH_KEY",
        "ANTHROPIC_KEY",
        "GEMINI_KEY",
        "COHERE_KEY",
    ):
        monkeypatch.setenv(name, f"{name.lower()}-test-secret")


def base_config(tmp_path: Path) -> AppConfig:
    config = load_config()
    config.storage.data_dir = str(tmp_path / "data")
    config.gateway.default_model = "alpha:one"
    config.gateway.fallback_models = []
    config.gateway.max_retries = 0
    config.gateway.retry_backoff_s = 0
    config.gateway.retry_max_backoff_s = 0
    config.gateway.rate_limit_rpm = 0
    config.gateway.first_token_timeout_s = 3
    config.gateway.request_timeout_s = 3
    config.gateway.max_output_tokens = 256
    config.memory = MemoryConfig(enabled=False)
    config.search = SearchConfig(enabled=False)
    config.providers = {
        "alpha": ProviderConfig(
            type="openai",
            base_url="http://alpha.test/v1",
            api_key_env="ALPHA_KEY",
        ),
        "beta": ProviderConfig(
            type="openai",
            base_url="http://beta.test/v1",
            api_key_env="BETA_KEY",
        ),
        "vision": ProviderConfig(
            type="openai",
            base_url="http://vision.test/v1",
            api_key_env="VISION_KEY",
        ),
        "echo": ProviderConfig(type="echo", requires_key=False, enabled=True),
    }
    config.models = [
        ModelConfig(
            id="alpha:one",
            provider="alpha",
            model="one",
            label="Alpha One",
            input_modalities=["text"],
            context_window=8192,
            fallbacks=["beta:one"],
        ),
        ModelConfig(
            id="beta:one",
            provider="beta",
            model="one",
            label="Beta One",
            input_modalities=["text"],
            context_window=8192,
        ),
        ModelConfig(
            id="alpha:vision",
            provider="alpha",
            model="vision",
            label="Alpha Vision",
            input_modalities=["text", "image"],
            context_window=8192,
        ),
        ModelConfig(
            id="vision:one",
            provider="vision",
            model="one",
            label="Vision One",
            input_modalities=["text", "image"],
            context_window=8192,
        ),
        ModelConfig(
            id="echo:echo",
            provider="echo",
            model="echo",
            label="Echo",
            input_modalities=["text"],
            context_window=8192,
        ),
    ]
    return config


def sse_response(
    text: str,
    *,
    chunk_size: int = 6,
    model: str = "one",
    usage: dict[str, int] | None = None,
) -> httpx.Response:
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)] or [""]
    payloads: list[dict[str, Any]] = [
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
        }
        for chunk in chunks
    ]
    payloads.append(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }
    )
    body = "".join(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n" for payload in payloads)
    body += "data: [DONE]\n\n"
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body.encode("utf-8"),
    )


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content.decode("utf-8"))

    def with_host(self, host: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.host == host]


def make_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@asynccontextmanager
async def app_client(
    config: AppConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    memory_plugin: Any = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    app = create_app(config, transport=transport, memory_plugin=memory_plugin)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        yield client, app
