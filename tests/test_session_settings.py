"""Session-level settings endpoint used by desktop clients."""

from __future__ import annotations

import pytest

from tests.conftest import app_client, base_config


def echo_config(tmp_path):
    config = base_config(tmp_path)
    config.gateway.default_model = "echo:echo"
    config.gateway.fallback_models = []
    config.models[0].fallbacks = []
    return config


@pytest.mark.asyncio
async def test_session_settings_expose_search_switch_without_history(tmp_path) -> None:
    config = echo_config(tmp_path)
    async with app_client(config) as (client, _):
        created = await client.post(
            "/v1/sessions",
            json={"title": "search follow", "model": "echo:echo", "web_search": True},
        )
        assert created.status_code == 201
        session_id = created.json()["session"]["id"]

        settings = await client.get(f"/v1/sessions/{session_id}/settings")
        assert settings.status_code == 200
        body = settings.json()
        assert body["session_id"] == session_id
        assert body["web_search"] is True
        # 桌面端每轮都会读它，不能把消息历史一起带回去。
        assert "messages" not in body
        assert "citations" not in body

        await client.patch(f"/v1/sessions/{session_id}", json={"web_search": False})
        settings = await client.get(f"/v1/sessions/{session_id}/settings")
        assert settings.json()["web_search"] is False


@pytest.mark.asyncio
async def test_session_settings_missing_session_returns_404(tmp_path) -> None:
    async with app_client(echo_config(tmp_path)) as (client, _):
        response = await client.get("/v1/sessions/does-not-exist/settings")
        assert response.status_code == 404
