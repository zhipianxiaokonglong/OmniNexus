"""Versioned, model-independent prompt profiles.

Every update creates an immutable version. The HTTP layer exposes read/set/
activate/reset/delete operations for the future UI. The built-in default is
deliberately blank: no production persona is shipped in this repository.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from typing import Any

from jinja2 import StrictUndefined, UndefinedError, meta
from jinja2.sandbox import SandboxedEnvironment

from omninexus.config import PromptConfig
from omninexus.core.models import iso_now
from omninexus.errors import ErrorCode, NotFoundError, OmniError
from omninexus.logging import get_logger
from omninexus.storage.database import Database

log = get_logger("prompts")

DEFAULT_PROFILE_ID = "default"
DEFAULT_PROFILE_NAME = "default"
DEFAULT_DESCRIPTION = (
    "Built-in blank system prompt. The formal persona/prompt is intentionally "
    "not shipped; fill it after product development via the prompt API."
)


class PromptValidationError(OmniError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.PROMPT_INVALID, message, status_code=422)


def _hash(template: str) -> str:
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


class PromptManager:
    def __init__(self, db: Database, config: PromptConfig) -> None:
        self.db = db
        self.config = config
        self._env = SandboxedEnvironment(autoescape=False, undefined=StrictUndefined)

    async def initialize(self) -> None:
        existing = await self.db.fetchone(
            "SELECT id FROM prompt_profiles WHERE id = ?", (DEFAULT_PROFILE_ID,)
        )
        if existing:
            return
        now = iso_now()
        version_id = str(uuid.uuid4())
        template = ""

        def _seed(conn: sqlite3.Connection) -> None:
            conn.execute(
                """INSERT OR IGNORE INTO prompt_profiles
                   (id, name, description, builtin, active_version, created_at, updated_at)
                   VALUES(?, ?, ?, 1, 1, ?, ?)""",
                (DEFAULT_PROFILE_ID, DEFAULT_PROFILE_NAME, DEFAULT_DESCRIPTION, now, now),
            )
            conn.execute(
                """INSERT OR IGNORE INTO prompt_versions
                   (id, profile_id, version, system_template, change_note,
                    content_hash, created_at)
                   VALUES(?, ?, 1, ?, 'built-in blank default', ?, ?)""",
                (version_id, DEFAULT_PROFILE_ID, template, _hash(template), now),
            )

        await self.db.transaction(_seed)

    def validate_template(self, template: str) -> None:
        if not isinstance(template, str):
            raise PromptValidationError("system_template must be a string")
        if len(template) > self.config.max_template_chars:
            raise PromptValidationError(
                f"system_template exceeds {self.config.max_template_chars} characters"
            )
        try:
            parsed = self._env.parse(template)
            meta.find_undeclared_variables(parsed)
        except Exception as exc:
            raise PromptValidationError(f"invalid prompt template: {type(exc).__name__}") from exc

    def _render(self, template: str, variables: dict[str, Any]) -> str:
        if not template:
            return ""
        try:
            return self._env.from_string(template).render(**variables)
        except UndefinedError:
            # Unknown variables must not break a conversation; render them empty.
            env = SandboxedEnvironment(autoescape=False)
            try:
                return env.from_string(template).render(**variables)
            except Exception as exc:  # pragma: no cover - syntax was validated
                log.warning("prompt_render_failed type=%s", type(exc).__name__)
                return ""

    async def get_active_template(
        self, profile_id: str | None, variables: dict[str, Any]
    ) -> tuple[str, str]:
        """Return (rendered_template, profile_id), falling back to the default."""
        profile_id = profile_id or self.config.default_profile or DEFAULT_PROFILE_ID
        row = await self.db.fetchone(
            "SELECT active_version FROM prompt_profiles WHERE id = ?", (profile_id,)
        )
        if not row:
            profile_id = self.config.default_profile or DEFAULT_PROFILE_ID
            row = await self.db.fetchone(
                "SELECT active_version FROM prompt_profiles WHERE id = ?", (profile_id,)
            )
        if not row:
            return "", profile_id
        version = await self.db.fetchone(
            "SELECT system_template FROM prompt_versions WHERE profile_id = ? AND version = ?",
            (profile_id, row["active_version"]),
        )
        if not version:
            return "", profile_id
        return self._render(version["system_template"], variables), profile_id

    async def list_profiles(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """SELECT p.*, v.system_template, v.version AS active_version_number,
                      v.change_note, v.created_at AS version_created_at
               FROM prompt_profiles p
               LEFT JOIN prompt_versions v
                 ON v.profile_id = p.id AND v.version = p.active_version
               ORDER BY p.builtin DESC, p.created_at ASC"""
        )
        return [dict(row) for row in rows]

    async def get_profile(self, profile_id: str) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM prompt_profiles WHERE id = ?", (profile_id,))
        if not row:
            raise NotFoundError(ErrorCode.PROMPT_NOT_FOUND, "Prompt profile not found")
        version = await self.db.fetchone(
            "SELECT * FROM prompt_versions WHERE profile_id = ? AND version = ?",
            (profile_id, row["active_version"]),
        )
        versions = await self.db.fetchall(
            """SELECT version, change_note, content_hash, created_at
               FROM prompt_versions WHERE profile_id = ? ORDER BY version DESC""",
            (profile_id,),
        )
        result = dict(row)
        result["system_template"] = version["system_template"] if version else ""
        result["versions"] = [dict(v) for v in versions]
        return result

    async def create_profile(
        self, *, name: str, description: str, system_template: str, activate: bool = True
    ) -> dict[str, Any]:
        if not self.config.allow_custom_profiles:
            raise PromptValidationError("custom prompt profiles are disabled")
        name = name.strip()
        if not name or len(name) > 80:
            raise PromptValidationError("name must be 1-80 characters")
        self.validate_template(system_template)
        profile_id = str(uuid.uuid4())
        now = iso_now()
        version_id = str(uuid.uuid4())

        def _create(conn: sqlite3.Connection) -> None:
            conn.execute(
                """INSERT INTO prompt_profiles
                   (id, name, description, builtin, active_version, created_at, updated_at)
                   VALUES(?, ?, ?, 0, 1, ?, ?)""",
                (profile_id, name, description[:500], now, now),
            )
            conn.execute(
                """INSERT INTO prompt_versions
                   (id, profile_id, version, system_template, change_note,
                    content_hash, created_at)
                   VALUES(?, ?, 1, ?, 'initial version', ?, ?)""",
                (version_id, profile_id, system_template, _hash(system_template), now),
            )
            if not activate:
                conn.execute(
                    "UPDATE prompt_profiles SET updated_at = ? WHERE id = ?", (now, profile_id)
                )

        try:
            await self.db.transaction(_create)
        except sqlite3.IntegrityError as exc:
            raise PromptValidationError("a prompt profile with this name already exists") from exc
        return await self.get_profile(profile_id)

    async def update_profile(
        self,
        profile_id: str,
        *,
        system_template: str,
        change_note: str = "",
    ) -> dict[str, Any]:
        self.validate_template(system_template)
        now = iso_now()
        version_id = str(uuid.uuid4())

        def _update(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT active_version FROM prompt_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(ErrorCode.PROMPT_NOT_FOUND, "Prompt profile not found")
            next_version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM prompt_versions WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO prompt_versions
                   (id, profile_id, version, system_template, change_note,
                    content_hash, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    version_id,
                    profile_id,
                    next_version,
                    system_template,
                    change_note[:500],
                    _hash(system_template),
                    now,
                ),
            )
            conn.execute(
                """UPDATE prompt_profiles SET active_version = ?, updated_at = ? WHERE id = ?""",
                (next_version, now, profile_id),
            )
            return next_version

        await self.db.transaction(_update)
        return await self.get_profile(profile_id)

    async def activate_version(self, profile_id: str, version: int) -> dict[str, Any]:
        now = iso_now()

        def _activate(conn: sqlite3.Connection) -> None:
            profile = conn.execute(
                "SELECT id FROM prompt_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if not profile:
                raise NotFoundError(ErrorCode.PROMPT_NOT_FOUND, "Prompt profile not found")
            exists = conn.execute(
                "SELECT 1 FROM prompt_versions WHERE profile_id = ? AND version = ?",
                (profile_id, version),
            ).fetchone()
            if not exists:
                raise NotFoundError(ErrorCode.PROMPT_VERSION_NOT_FOUND, "Prompt version not found")
            conn.execute(
                "UPDATE prompt_profiles SET active_version = ?, updated_at = ? WHERE id = ?",
                (version, now, profile_id),
            )

        await self.db.transaction(_activate)
        return await self.get_profile(profile_id)

    async def reset_profile(self, profile_id: str) -> dict[str, Any]:
        """Create and activate a new version equal to the profile's version 1."""
        now = iso_now()
        version_id = str(uuid.uuid4())

        def _reset(conn: sqlite3.Connection) -> None:
            first = conn.execute(
                "SELECT system_template FROM prompt_versions WHERE profile_id = ? AND version = 1",
                (profile_id,),
            ).fetchone()
            profile = conn.execute(
                "SELECT id FROM prompt_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if not profile or not first:
                raise NotFoundError(ErrorCode.PROMPT_NOT_FOUND, "Prompt profile not found")
            next_version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM prompt_versions WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO prompt_versions
                   (id, profile_id, version, system_template, change_note,
                    content_hash, created_at)
                   VALUES(?, ?, ?, ?, 'reset to version 1', ?, ?)""",
                (
                    version_id,
                    profile_id,
                    next_version,
                    first["system_template"],
                    _hash(first["system_template"]),
                    now,
                ),
            )
            conn.execute(
                "UPDATE prompt_profiles SET active_version = ?, updated_at = ? WHERE id = ?",
                (next_version, now, profile_id),
            )

        await self.db.transaction(_reset)
        return await self.get_profile(profile_id)

    async def delete_profile(self, profile_id: str) -> None:
        if profile_id == DEFAULT_PROFILE_ID:
            raise OmniError(
                ErrorCode.PROMPT_BUILTIN_PROTECTED,
                "The built-in default prompt profile cannot be deleted.",
                status_code=409,
            )
        row = await self.db.fetchone(
            "SELECT builtin FROM prompt_profiles WHERE id = ?", (profile_id,)
        )
        if not row:
            raise NotFoundError(ErrorCode.PROMPT_NOT_FOUND, "Prompt profile not found")
        if row["builtin"]:
            raise OmniError(
                ErrorCode.PROMPT_BUILTIN_PROTECTED,
                "Built-in prompt profiles cannot be deleted.",
                status_code=409,
            )
        await self.db.execute("DELETE FROM prompt_profiles WHERE id = ?", (profile_id,))

    def preview(self, template: str, variables: dict[str, Any]) -> str:
        self.validate_template(template)
        base: dict[str, Any] = {
            "current_time": iso_now(),
            "locale": "zh-CN",
            "model": "provider:model",
            "session_id": "00000000-0000-0000-0000-000000000000",
            "user_input": "示例输入",
        }
        base.update({str(k): v for k, v in variables.items() if isinstance(k, str)})
        return self._render(template, base)

    @staticmethod
    def safe_variables(variables: dict[str, Any]) -> dict[str, Any]:
        """Only JSON scalar values are exposed to templates."""
        clean: dict[str, Any] = {}
        for key, value in variables.items():
            if not isinstance(key, str) or not key.isidentifier():
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                clean[key[:64]] = value if not isinstance(value, str) else value[:4000]
        return clean
