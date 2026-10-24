"""Server-Sent Events encoding and heartbeat-aware stream wrapper."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from omninexus.orchestrator import Orchestrator, PreparedTurn


def encode_sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


async def event_stream(
    orchestrator: Orchestrator, prepared: PreparedTurn, *, heartbeat_s: float = 15.0
) -> AsyncIterator[str]:
    iterator = orchestrator.stream_prepared(prepared).__aiter__()
    try:
        while True:
            try:
                async with asyncio.timeout(heartbeat_s):
                    frame = await anext(iterator)
            except StopAsyncIteration:
                break
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - last-resort stream boundary
                yield encode_sse(
                    "error",
                    {
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "The stream ended unexpectedly.",
                            "retryable": True,
                        }
                    },
                )
                break
            yield encode_sse(frame.event, frame.data)
    finally:
        await iterator.aclose()
