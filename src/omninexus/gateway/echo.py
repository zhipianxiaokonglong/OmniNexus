"""Built-in offline diagnostic provider.

This is not a persona and is disabled by default. It exists so a fresh checkout
can demonstrate the complete input → event → tags pipeline without a key.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from omninexus.core.models import ChatDelta, LLMMessage, TokenUsage
from omninexus.core.tokenizer import estimate_tokens
from omninexus.gateway.base import BaseProvider


class EchoProvider(BaseProvider):
    type_name = "echo"

    async def stream(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        max_tokens: int,
        temperature: float,
        top_p: float,
    ) -> AsyncIterator[ChatDelta]:
        user_messages = [m for m in messages if m.role == "user"]
        last = (user_messages[-1].content if user_messages else "").strip()
        last = " ".join(last.split())[:500]
        text = (
            "Offline diagnostic echo (no external model was called).\n\n"
            f"Your message: {last or '(empty)'}\n\n"
            f"Conversation messages in context: {len(messages)}. "
            "Configure a provider key in .env and switch models to use a real LLM."
        )
        tokens = max(1, estimate_tokens(text) // 10)
        for index in range(0, len(text), tokens):
            yield ChatDelta(text=text[index : index + tokens])
            await asyncio.sleep(0)
        prompt_tokens = sum(estimate_tokens(m.content) for m in messages)
        completion_tokens = estimate_tokens(text)
        yield ChatDelta(
            text="",
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            finish_reason="stop",
        )
