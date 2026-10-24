"""Prompt API: profiles, immutable versions, activation, reset and safety."""

from __future__ import annotations

import pytest

from tests.conftest import app_client, base_config


@pytest.mark.asyncio
async def test_prompt_profile_version_lifecycle(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config) as (client, _):
        response = await client.get("/v1/prompts/profiles")
        assert response.status_code == 200
        profiles = response.json()["profiles"]
        assert any(item["id"] == "default" and item["builtin"] == 1 for item in profiles)
        default = next(item for item in profiles if item["id"] == "default")
        assert default["system_template"] == ""

        created = await client.post(
            "/v1/prompts/profiles",
            json={
                "name": "work-notes",
                "description": "test fixture",
                "system_template": "Locale={{ locale }}. User said: {{ user_input }}",
            },
        )
        assert created.status_code == 201
        profile = created.json()["profile"]
        profile_id = profile["id"]
        assert profile["active_version"] == 1

        preview = await client.post(
            "/v1/prompts/preview",
            json={
                "system_template": "Now={{ current_time }}; M={{ model }}",
                "variables": {"model": "alpha:one"},
            },
        )
        assert preview.status_code == 200
        assert "alpha:one" in preview.json()["rendered"]

        updated = await client.put(
            f"/v1/prompts/profiles/{profile_id}",
            json={"system_template": "Version two {{ user_input }}", "change_note": "v2"},
        )
        assert updated.status_code == 200
        assert updated.json()["profile"]["active_version"] == 2
        assert len(updated.json()["profile"]["versions"]) == 2

        activated = await client.post(
            f"/v1/prompts/profiles/{profile_id}/activate", json={"version": 1}
        )
        assert activated.status_code == 200
        assert activated.json()["profile"]["active_version"] == 1

        reset = await client.post(f"/v1/prompts/profiles/{profile_id}/reset")
        assert reset.status_code == 200
        assert reset.json()["profile"]["active_version"] == 3
        assert reset.json()["profile"]["system_template"].startswith("Locale=")

        deleted = await client.delete(f"/v1/prompts/profiles/{profile_id}")
        assert deleted.status_code == 204
        missing = await client.get(f"/v1/prompts/profiles/{profile_id}")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_prompt_validation_and_builtin_protection(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config) as (client, _):
        invalid = await client.post(
            "/v1/prompts/profiles",
            json={"name": "bad", "system_template": "{% for %}"},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "PROMPT_INVALID"

        delete_builtin = await client.delete("/v1/prompts/profiles/default")
        assert delete_builtin.status_code == 409
        assert delete_builtin.json()["error"]["code"] == "PROMPT_BUILTIN_PROTECTED"

        update_builtin = await client.put(
            "/v1/prompts/profiles/default",
            json={"system_template": "Neutral operational instruction.", "change_note": "fill"},
        )
        assert update_builtin.status_code == 200
        assert update_builtin.json()["profile"]["active_version"] == 2

        reset = await client.post("/v1/prompts/profiles/default/reset")
        assert reset.status_code == 200
        assert reset.json()["profile"]["system_template"] == ""


@pytest.mark.asyncio
async def test_prompt_unknown_profile_is_404(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config) as (client, _):
        response = await client.get("/v1/prompts/profiles/does-not-exist")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PROMPT_NOT_FOUND"
