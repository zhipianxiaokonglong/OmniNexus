"""Local-at-rest encryption for custom-provider API keys."""

from __future__ import annotations

import base64
import contextlib
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from omninexus.errors import ErrorCode, OmniError
from omninexus.logging import get_logger, redact, register_secret

log = get_logger("secrets")
_PREFIX = "v1:"
_KEY_BYTES = 32


class SecretStoreError(OmniError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.SECRET_STORE_ERROR, message, status_code=500)


def mask_api_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    if len(key) <= 12:
        return f"{key[:2]}****{key[-2:]}"
    prefix = "sk-" if key.startswith("sk-") else key[:3]
    return f"{prefix}****{key[-4:]}"


class SecretBox:
    def __init__(self, key: bytes, *, source: str = "file") -> None:
        if len(key) != _KEY_BYTES:
            raise SecretStoreError("Provider secret key must be 32 bytes.")
        self._key = key
        self._aes = AESGCM(key)
        self.source = source

    @classmethod
    def load(cls, data_dir: str | Path, *, env_name: str = "OMNI_PROVIDER_SECRET") -> SecretBox:
        env_value = os.environ.get(env_name)
        if env_value:
            try:
                key = base64.urlsafe_b64decode(env_value.encode())
            except Exception as exc:
                raise SecretStoreError(f"{env_name} 不是合法的 base64 密钥") from exc
            if len(key) != _KEY_BYTES:
                raise SecretStoreError(f"{env_name} 解码后必须是 32 字节")
            return cls(key, source="env")

        path = Path(data_dir) / "provider-secrets.key"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            key = secrets.token_bytes(_KEY_BYTES)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            mode = 0o600 if os.name != "nt" else 0o666
            fd = os.open(path, flags, mode)
            try:
                os.write(fd, base64.urlsafe_b64encode(key))
            finally:
                os.close(fd)
            with contextlib.suppress(OSError):  # Windows ACLs
                path.chmod(0o600)
            log.info("provider_secret_key_created path=%s", path.name)
        raw = base64.urlsafe_b64decode(path.read_bytes())
        if len(raw) != _KEY_BYTES:
            raise SecretStoreError("Provider secret key file is corrupt.")
        return cls(raw, source="file")

    def encrypt(self, plaintext: str | None, *, aad: str) -> str:
        if not plaintext:
            return ""
        nonce = secrets.token_bytes(12)
        ciphertext = self._aes.encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
        token = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        register_secret(plaintext)
        return _PREFIX + token

    def decrypt(self, token: str | None, *, aad: str) -> str:
        if not token:
            return ""
        if not token.startswith(_PREFIX):
            raise SecretStoreError("Unknown provider secret format.")
        try:
            payload = base64.urlsafe_b64decode(token[len(_PREFIX) :].encode("ascii"))
            nonce, ciphertext = payload[:12], payload[12:]
            value = self._aes.decrypt(nonce, ciphertext, aad.encode("utf-8")).decode("utf-8")
        except Exception as exc:
            raise SecretStoreError("无法解密提供方密钥；主密钥可能已更换") from exc
        register_secret(value)
        return value


def safe_error_text(exc: Exception) -> str:
    """Error text for logs/API: redacts registered secrets and key patterns."""
    return redact(str(exc))
