"""Configuration loading, validation and non-secret model registry.

Configuration is YAML plus environment variables. API keys are **never** stored
in YAML: providers name an environment variable (``api_key_env``) that is read
at request time. Secrets are not exposed by the HTTP API.
"""

from __future__ import annotations

import os
import re
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from omninexus.core.models import ModelInfo
from omninexus.errors import ErrorCode, OmniError

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(OmniError):
    def __init__(self, message: str) -> None:
        super().__init__(
            ErrorCode.INTERNAL_ERROR, f"Configuration error: {message}", status_code=500
        )


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            return os.environ.get(name, default or "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = Field(default_factory=list)
    max_body_bytes: int = 20 * 1024 * 1024
    trusted_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost", "::1", "testserver"]
    )
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"
    log_file: str | None = None


class StorageConfig(BaseModel):
    data_dir: str = "./data"
    db_filename: str = "omninexus.db"
    keep_partial_on_disconnect: bool = True

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / self.db_filename


class GatewayConfig(BaseModel):
    default_model: str = "openai:gpt-4o-mini"
    fallback_models: list[str] = Field(default_factory=list)
    connect_timeout_s: float = 8.0
    request_timeout_s: float = 90.0
    first_token_timeout_s: float = 45.0
    max_retries: int = 2
    retry_backoff_s: float = 0.6
    retry_max_backoff_s: float = 8.0
    rate_limit_rpm: int = 120
    rate_limit_max_wait_s: float = 1.5
    max_output_tokens: int = 2048
    temperature: float = 0.7
    top_p: float = 1.0
    context_reserve_tokens: int = 512
    max_history_messages: int = 200
    citations_in_content: bool = False

    @field_validator("max_retries", "rate_limit_rpm", "max_output_tokens")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be >= 0")
        return value

    @field_validator("temperature")
    @classmethod
    def _temperature(cls, value: float) -> float:
        if not 0 <= value <= 2:
            raise ValueError("temperature must be between 0 and 2")
        return value


class MemoryConfig(BaseModel):
    enabled: bool = True
    base_url: str = ""
    contract_version: str = "1.0"
    health_path: str = "/health"
    query_path: str = "/v1/memory/query"
    write_path: str = "/v1/memory/write"
    auth_token_env: str = "OMNI_MEMORY_TOKEN"
    health_interval_s: float = 30.0
    health_timeout_ms: int = 600
    query_timeout_ms: int = 1200
    write_timeout_ms: int = 1500
    circuit_failures: int = 3
    circuit_cooldown_s: float = 30.0
    top_k: int = 5
    max_items: int = 8
    max_context_tokens: int = 700
    outbox_max: int = 2000
    async_writeback: bool = True

    @field_validator("auth_token_env")
    @classmethod
    def _env_name(cls, value: str) -> str:
        if value and not _ENV_NAME.match(value):
            raise ValueError("auth_token_env must be an environment variable name")
        return value

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.base_url)


class TavilyConfig(BaseModel):
    api_key_env: str = "TAVILY_API_KEY"
    base_url: str = "https://api.tavily.com"


class BraveConfig(BaseModel):
    api_key_env: str = "BRAVE_SEARCH_API_KEY"
    base_url: str = "https://api.search.brave.com"


class SearxngConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8888"


class CustomSearchConfig(BaseModel):
    endpoint: str = ""
    api_key_env: str = ""
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"


class SearchConfig(BaseModel):
    enabled: bool = False
    provider: Literal["none", "tavily", "brave", "searxng", "duckduckgo", "custom"] = "none"
    top_k: int = 5
    timeout_s: float = 10.0
    max_query_chars: int = 256
    tavily: TavilyConfig = Field(default_factory=TavilyConfig)
    brave: BraveConfig = Field(default_factory=BraveConfig)
    searxng: SearxngConfig = Field(default_factory=SearxngConfig)
    custom: CustomSearchConfig = Field(default_factory=CustomSearchConfig)

    @model_validator(mode="after")
    def _check_provider(self) -> SearchConfig:
        if self.enabled and self.provider == "custom" and not self.custom.endpoint:
            raise ValueError("search.provider=custom requires search.custom.endpoint")
        if self.enabled and self.provider != "none" and not 1 <= self.top_k <= 10:
            raise ValueError("search.top_k must be between 1 and 10")
        return self


class PromptConfig(BaseModel):
    default_profile: str = "default"
    max_template_chars: int = 32_000
    allow_custom_profiles: bool = True


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["openai", "anthropic", "gemini", "cohere", "echo"] = "openai"
    base_url: str = ""
    api_key_env: str = ""
    requires_key: bool = True
    enabled: bool = True
    headers: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    connect_timeout_s: float | None = None
    label: str = ""

    @field_validator("api_key_env")
    @classmethod
    def _env_name(cls, value: str) -> str:
        if value and not _ENV_NAME.match(value):
            raise ValueError("api_key_env must be an environment variable name")
        return value

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check(self) -> ProviderConfig:
        if self.type != "echo" and not self.base_url:
            raise ValueError("non-echo provider requires base_url")
        if self.type != "echo" and self.requires_key and not self.api_key_env:
            raise ValueError("provider requires api_key_env (BYOK) or requires_key=false")
        return self

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        value = os.environ.get(self.api_key_env)
        return value.strip() if value else None

    def is_configured(self) -> bool:
        if not self.enabled:
            return False
        if self.type == "echo":
            return True
        if self.requires_key:
            return bool(self.api_key())
        return True


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    provider: str
    model: str
    label: str = ""
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    context_window: int = 32_000
    max_output_tokens: int = 2048
    supports_streaming: bool = True
    supports_tools: bool = False
    fallbacks: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> ModelConfig:
        if ":" not in self.id:
            raise ValueError("model id must look like 'provider:model'")
        if self.context_window < 512:
            raise ValueError("context_window is too small")
        if not self.label:
            self.label = self.model
        return self


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    prompts: PromptConfig = Field(default_factory=PromptConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: list[ModelConfig] = Field(default_factory=list)

    config_path: Path | None = Field(default=None, exclude=True)
    base_dir: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _check_registry(self) -> AppConfig:
        ids: set[str] = set()
        for model in self.models:
            if model.id in ids:
                raise ValueError(f"duplicate model id: {model.id}")
            ids.add(model.id)
            if model.provider not in self.providers:
                raise ValueError(f"model {model.id} references unknown provider {model.provider}")
            if model.id.split(":", 1)[0] != model.provider:
                raise ValueError(f"model id prefix must match provider: {model.id}")
        for model in self.models:
            for fallback in model.fallbacks:
                if fallback not in ids:
                    raise ValueError(f"model {model.id} has unknown fallback {fallback}")
        for fallback in self.gateway.fallback_models:
            if fallback not in ids:
                raise ValueError(f"gateway fallback has unknown model {fallback}")
        if self.models and self.gateway.default_model not in ids:
            raise ValueError("gateway.default_model is not in the model registry")
        return self

    def model_config_by_id(self, model_id: str) -> ModelConfig | None:
        return next((model for model in self.models if model.id == model_id), None)

    def provider_config(self, name: str) -> ProviderConfig | None:
        return self.providers.get(name)

    def model_info(self, model: ModelConfig) -> ModelInfo:
        provider = self.providers[model.provider]
        configured = provider.is_configured()
        reason = None
        if not provider.enabled:
            reason = "provider_disabled"
        elif not configured:
            reason = "missing_api_key"
        return ModelInfo(
            id=model.id,
            label=model.label,
            provider=model.provider,
            upstream_model=model.model,
            input_modalities=list(model.input_modalities),
            output_modalities=list(model.output_modalities),
            context_window=model.context_window,
            max_output_tokens=model.max_output_tokens,
            supports_streaming=model.supports_streaming,
            supports_tools=model.supports_tools,
            available=configured,
            unavailable_reason=reason,
            fallbacks=list(model.fallbacks),
            metadata=model.metadata,
        )

    def available_models(self) -> list[ModelInfo]:
        return [self.model_info(model) for model in self.models]

    def resolve_data_dir(self) -> Path:
        path = Path(self.storage.data_dir)
        if not path.is_absolute():
            path = self.base_dir / path
        return path.resolve()


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("top-level configuration must be a mapping")
    return _expand_env(data)


def _find_config() -> Path:
    env_path = os.environ.get("OMNI_CONFIG")
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            raise ConfigError(f"OMNI_CONFIG points to a missing file: {path}")
        return path.resolve()

    candidates = [
        Path.cwd() / "config" / "default.yaml",
        Path(__file__).resolve().parents[2] / "config" / "default.yaml",
    ]
    for path in candidates:
        if path.exists():
            return path.resolve()

    # Installed wheel: the same default is bundled as package data.
    try:
        bundled = Path(str(files("omninexus").joinpath("resources", "default.yaml")))
    except (ModuleNotFoundError, TypeError):  # pragma: no cover - defensive
        bundled = Path("")
    if bundled.exists():
        return bundled.resolve()
    raise ConfigError(
        "config/default.yaml not found. Run from the repository root or set OMNI_CONFIG."
    )


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load `.env` (local only), YAML, then apply narrow environment overrides."""
    load_dotenv(Path.cwd() / ".env", override=False)

    config_path = Path(path).resolve() if path else _find_config()
    raw = _read_yaml(config_path)

    try:
        config = AppConfig.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc

    config.config_path = config_path
    if config_path.parent.name == "config":
        config.base_dir = config_path.parent.parent
    elif config_path.parent.name == "resources":
        # Installed package data is read-only; keep runtime data in the cwd.
        config.base_dir = Path.cwd()
    else:
        config.base_dir = config_path.parent

    if os.environ.get("OMNI_HOST"):
        config.server.host = os.environ["OMNI_HOST"]
    if os.environ.get("OMNI_PORT"):
        try:
            config.server.port = int(os.environ["OMNI_PORT"])
        except ValueError as exc:
            raise ConfigError("OMNI_PORT must be an integer") from exc
    if os.environ.get("OMNI_DATA_DIR"):
        config.storage.data_dir = os.environ["OMNI_DATA_DIR"]
    if os.environ.get("OMNI_MEMORY_BASE_URL"):
        config.memory.base_url = os.environ["OMNI_MEMORY_BASE_URL"].rstrip("/")
    if os.environ.get("SEARXNG_BASE_URL"):
        config.search.searxng.base_url = os.environ["SEARXNG_BASE_URL"].rstrip("/")
    if os.environ.get("OMNI_MEMORY_ENABLED", "").lower() in {"1", "true", "yes", "on"}:
        config.memory.enabled = True
    if os.environ.get("OMNI_ENABLE_ECHO", "").lower() in {"1", "true", "yes", "on"}:
        echo = config.providers.get("echo")
        if echo:
            echo.enabled = True
    if os.environ.get("OMNI_ENABLE_LOCAL", "").lower() in {"1", "true", "yes", "on"}:
        for local_name in ("ollama", "lmstudio", "vllm", "llamacpp"):
            provider = config.providers.get(local_name)
            if provider:
                provider.enabled = True

    data_dir = config.resolve_data_dir()
    config.storage.data_dir = str(data_dir)
    return config
