"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from omninexus import __version__
from omninexus.api.provider_routes import router as provider_router
from omninexus.api.routes import router
from omninexus.config import AppConfig, load_config
from omninexus.errors import ErrorCode, OmniError
from omninexus.gateway.router import Gateway
from omninexus.logging import configure_logging, get_logger
from omninexus.memory.plugin import MemoryPlugin, build_memory_plugin
from omninexus.orchestrator import Orchestrator
from omninexus.prompts.manager import PromptManager
from omninexus.providers.secrets import SecretBox
from omninexus.providers.service import ProviderService
from omninexus.providers.store import CustomProviderStore
from omninexus.search.service import SearchService
from omninexus.sessions.service import SessionService
from omninexus.storage.database import Database

log = get_logger("app")
_STATIC = Path(__file__).parent / "static"


_MODEL_LIMIT_MESSAGE = "模型数量超过上限：单个提供方最多 200 个模型，请减少模型后重试。"
_VALIDATION_MESSAGES = {
    "name": "提供方名称不合法，请检查长度。",
    "base_url": "Base URL 不合法，请填写 http(s) 绝对地址。",
    "api_key": "API Key 不合法或过长。",
    "default_model": "默认模型不合法。",
    "timeout_seconds": "测试超时必须为 1–120 秒。",
}


def _validation_message(fields: list[dict[str, Any]]) -> str:
    """Turn the first field error into an actionable message.

    FastAPI/Pydantic errors already contain the real cause, but exposing a fixed
    "Request validation failed." made the 200-model limit undiscoverable.
    """
    if not fields:
        return "请求参数校验失败。"
    first = fields[0]
    field = str(first.get("field") or "")
    reason = str(first.get("reason") or "").strip()
    root = field.split(".", 1)[0]
    if field == "models":
        if "items" in reason or "too_long" in reason:
            return _MODEL_LIMIT_MESSAGE
        if "at least" in reason or "too_short" in reason:
            return "至少需要添加一个模型。"
    # Pydantic may report either the alias or the Python field name.
    alias = {
        "baseUrl": "base_url",
        "apiKey": "api_key",
        "defaultModel": "default_model",
        "timeoutSeconds": "timeout_seconds",
    }.get(root, root)
    friendly = _VALIDATION_MESSAGES.get(alias)
    if friendly:
        return friendly
    if field and reason:
        return f"参数 {field} 校验失败：{reason}"
    return "请求参数校验失败。"


def _json_error(
    code: ErrorCode,
    message: str,
    status_code: int,
    request: Request | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "error": {"code": code.value, "message": message, "retryable": False}
    }
    if details:
        payload["error"]["details"] = details
    if request is not None:
        payload["error"]["request_id"] = getattr(request.state, "request_id", "")
        payload["request_id"] = payload["error"]["request_id"]
    return JSONResponse(payload, status_code=status_code)


def create_app(
    config: AppConfig | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    memory_plugin: MemoryPlugin | None = None,
    search_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    config = config or load_config()
    configure_logging(config)

    db = Database(config.storage.db_path)
    secret_box = SecretBox.load(config.resolve_data_dir())
    provider_store = CustomProviderStore(db, secret_box)
    provider_service = ProviderService(config, provider_store)
    gateway = Gateway(
        config,
        transport=transport,
        provider_service=provider_service,
    )
    prompts = PromptManager(db, config.prompts)
    sessions = SessionService(db, config, provider_service)
    memory = memory_plugin or build_memory_plugin(config.memory, transport=transport)
    search = SearchService(config.search, transport=search_transport or transport)
    orchestrator = Orchestrator(
        config=config,
        db=db,
        gateway=gateway,
        prompts=prompts,
        sessions=sessions,
        memory=memory,
        search=search,
        provider_service=provider_service,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await db.initialize()
        await prompts.initialize()
        await provider_service.reload()
        await gateway.refresh_custom_providers()
        if config.server.host not in {"127.0.0.1", "localhost", "::1"}:
            log.warning(
                "server binds to a non-loopback address without built-in authentication; "
                "use a firewall or reverse-proxy auth"
            )
        if config.memory.enabled and not config.memory.is_configured:
            log.warning("reserved memory extension is enabled but base_url is empty")
        log.info(
            "omninexus_started version=%s models=%s archive=local memory=%s search=%s",
            __version__,
            len(config.models),
            "reserved",
            config.search.provider if config.search.enabled else "disabled",
        )
        try:
            yield
        finally:
            await orchestrator.close()
            log.info("omninexus_stopped")

    app = FastAPI(
        title="OmniNexus",
        description=(
            "Local-first LLM orchestration core: input events → model gateway → "
            "tagged output events, with archives, prompt versions, optional web search "
            "and a reserved memory-plugin interface."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.db = db
    app.state.gateway = gateway
    app.state.provider_store = provider_store
    app.state.provider_service = provider_service
    app.state.provider_secrets = secret_box
    app.state.provider_transport = transport
    app.state.prompts = prompts
    app.state.sessions = sessions
    app.state.memory = memory
    app.state.search = search
    app.state.orchestrator = orchestrator

    if config.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "Accept", "Last-Event-ID", "X-Request-ID"],
        )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.server.trusted_hosts)

    @app.middleware("http")
    async def request_guard(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("x-request-id", "")
        if not request_id or len(request_id) > 64 or not request_id.replace("-", "").isalnum():
            request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.monotonic()
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > config.server.max_body_bytes:
                    return _json_error(
                        ErrorCode.PAYLOAD_TOO_LARGE,
                        "Request body is too large.",
                        413,
                        request,
                        {"max_bytes": config.server.max_body_bytes},
                    )
            except ValueError:
                return _json_error(ErrorCode.INVALID_INPUT, "Invalid Content-Length.", 400, request)
        try:
            response = await call_next(request)
        except OmniError:
            raise
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path in {"/", "/settings"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'"
            )
        duration = int((time.monotonic() - started) * 1000)
        log.info(
            "request method=%s path=%s status=%s duration_ms=%s request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration,
            request_id,
            extra={
                "request_id": request_id,
                "duration_ms": duration,
            },
        )
        return response

    @app.exception_handler(OmniError)
    async def omni_error_handler(request: Request, exc: OmniError) -> JSONResponse:
        payload = exc.as_dict()
        request_id = getattr(request.state, "request_id", "")
        # The error contract advertises request_id inside the body; keep the
        # header too so proxies and clients can use either.
        payload["error"]["request_id"] = request_id
        payload["request_id"] = request_id
        response = JSONResponse(payload, status_code=exc.status_code)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", []) if part != "body")
            fields.append({"field": location[:120], "reason": str(error.get("msg"))[:200]})
        message = _validation_message(fields)
        return _json_error(
            ErrorCode.INVALID_INPUT,
            message,
            422,
            request,
            {"fields": fields[:10]},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INTERNAL_ERROR
        message = "Resource not found." if exc.status_code == 404 else "Request failed."
        return _json_error(code, message, exc.status_code, request)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_error type=%s", type(exc).__name__)
        return _json_error(
            ErrorCode.INTERNAL_ERROR,
            "An internal error occurred.",
            500,
            request,
        )

    app.include_router(router)
    app.include_router(provider_router)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    @app.get("/settings", include_in_schema=False)
    async def settings_page() -> FileResponse:
        return FileResponse(_STATIC / "settings.html")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(_STATIC / "favicon.svg", media_type="image/svg+xml")

    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
    return app


# Uvicorn factory entry point: `uvicorn omninexus.app:create_app --factory`
__all__ = ["create_app"]
