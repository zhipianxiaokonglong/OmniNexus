"""HTTP API routes."""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from omninexus import __version__
from omninexus.api.schemas import (
    MemoryProbe,
    ModelSwitch,
    ProfileCreate,
    ProfileUpdate,
    PromptPreview,
    PromptSwitch,
    SessionCreate,
    SessionUpdate,
    VersionActivate,
)
from omninexus.api.sse import event_stream
from omninexus.core.models import InboundEvent, SessionRecord, iso_now
from omninexus.errors import ErrorCode, OmniError
from omninexus.prompts.manager import PromptManager
from omninexus.sessions.service import export_filename

router = APIRouter(prefix="/v1")


def _orchestrator(request: Request) -> Any:
    return request.app.state.orchestrator


def _disposition(filename: str, session_id: str, extension: str) -> str:
    """HTTP headers are latin-1; provide an ASCII fallback plus RFC 5987 name."""
    fallback = f"omninexus-{session_id[:8]}.{extension}"
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _validate_model(request: Request, model_id: str) -> None:
    if not request.app.state.config.model_config_by_id(model_id):
        raise OmniError(
            ErrorCode.UNKNOWN_MODEL,
            "Unknown model id.",
            status_code=404,
            details={"model": model_id},
        )


async def _validate_profile(request: Request, profile_id: str) -> None:
    await request.app.state.prompts.get_profile(profile_id)


@router.post("/events", summary="Process one upstream event")
async def create_event(request: Request, event: InboundEvent) -> Response:
    orchestrator = _orchestrator(request)
    accept = request.headers.get("accept", "")
    stream_param = request.query_params.get("stream")
    streaming = "text/event-stream" in accept
    if stream_param is not None:
        streaming = stream_param.lower() not in {"0", "false", "no"}

    if streaming:
        prepared = await orchestrator.prepare(event)
        headers = {
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "X-Event-Id": prepared.output_event_id,
            "X-Session-Id": prepared.session.id,
        }
        return StreamingResponse(
            event_stream(orchestrator, prepared),
            media_type="text/event-stream; charset=utf-8",
            headers=headers,
        )

    result = await orchestrator.run_json(event)
    return JSONResponse(
        result.output.model_dump(),
        headers={
            "X-Event-Id": result.output.event_id,
            "X-Session-Id": result.output.session_id,
            "X-Citations": str(len(result.citations)),
        },
    )


# ----- sessions ---------------------------------------------------------
@router.post("/sessions", status_code=201)
async def create_session(request: Request, body: SessionCreate) -> dict[str, Any]:
    config = request.app.state.config
    model = body.model or config.gateway.default_model
    _validate_model(request, model)
    profile_id = body.prompt_profile or config.prompts.default_profile
    await _validate_profile(request, profile_id)
    session_id = str(uuid.uuid4())
    now = iso_now()
    record = SessionRecord(
        id=session_id,
        title=body.title or "New conversation",
        model=model,
        prompt_profile=profile_id,
        web_search=body.web_search,
        created_at=now,
        updated_at=now,
    )
    await request.app.state.db.create_session(record)
    return {"session": record.model_dump()}


@router.get("/sessions")
async def list_sessions(
    request: Request, limit: int = Query(default=100, ge=1, le=200), offset: int = 0
) -> dict[str, Any]:
    items = await request.app.state.sessions.list(limit=limit, offset=max(0, offset))
    return {"sessions": items, "limit": limit, "offset": max(0, offset)}


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: str) -> dict[str, Any]:
    return await request.app.state.sessions.detail(session_id)


@router.patch("/sessions/{session_id}")
async def update_session(request: Request, session_id: str, body: SessionUpdate) -> dict[str, Any]:
    fields = {
        key: value
        for key, value in body.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if "model" in fields:
        _validate_model(request, fields["model"])
    if "prompt_profile" in fields:
        await _validate_profile(request, fields["prompt_profile"])
    if not fields:
        return {"session": (await request.app.state.sessions.get(session_id)).model_dump()}
    record = await request.app.state.sessions.update(session_id, fields)
    return {"session": record.model_dump()}


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(request: Request, session_id: str) -> Response:
    await request.app.state.sessions.delete(session_id)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/model")
async def switch_model(request: Request, session_id: str, body: ModelSwitch) -> dict[str, Any]:
    _validate_model(request, body.model)
    record = await request.app.state.sessions.update(session_id, {"model": body.model})
    return {"session": record.model_dump()}


@router.post("/sessions/{session_id}/prompt")
async def switch_prompt(request: Request, session_id: str, body: PromptSwitch) -> dict[str, Any]:
    await _validate_profile(request, body.profile_id)
    record = await request.app.state.sessions.update(
        session_id, {"prompt_profile": body.profile_id}
    )
    return {"session": record.model_dump()}


@router.get("/sessions/{session_id}/export")
async def export_session(
    request: Request,
    session_id: str,
    format: str = Query(default="json", pattern="^(json|md)$"),
) -> Response:
    session = await request.app.state.sessions.get(session_id)
    if format == "md":
        content = await request.app.state.sessions.export_markdown(session_id)
        filename = export_filename(session.title, "md")
        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": _disposition(filename, session_id, "md")},
        )
    payload = await request.app.state.sessions.export_json(session_id)
    filename = export_filename(session.title, "json")
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": _disposition(filename, session_id, "json")},
    )


@router.get("/sessions/{session_id}/citations")
async def session_citations(request: Request, session_id: str) -> dict[str, Any]:
    await request.app.state.sessions.get(session_id)
    items = await request.app.state.db.get_session_citations(session_id)
    return {"citations": items}


@router.get("/events/{event_id}/citations")
async def event_citations(request: Request, event_id: str) -> dict[str, Any]:
    items = await request.app.state.db.get_citations(event_id)
    if not items:
        return {"citations": []}
    return {"citations": items}


# ----- models & providers ----------------------------------------------
@router.get("/models")
async def list_models(request: Request) -> dict[str, Any]:
    config = request.app.state.config
    models = []
    for model in config.available_models():
        item = model.model_dump()
        provider = config.providers.get(model.provider)
        item["provider_label"] = provider.label if provider else model.provider
        models.append(item)
    return {"models": models, "default_model": config.gateway.default_model}


@router.get("/providers")
async def list_providers(request: Request) -> dict[str, Any]:
    config = request.app.state.config
    items = []
    for name, provider in config.providers.items():
        parts = urlsplit(provider.base_url) if provider.base_url else None
        safe_url = ""
        if parts:
            netloc = parts.hostname or ""
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            safe_url = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        items.append(
            {
                "name": name,
                "type": provider.type,
                "label": provider.label or name,
                "base_url": safe_url,
                "api_key_env": provider.api_key_env,
                "requires_key": provider.requires_key,
                "configured": provider.is_configured(),
                "local": not provider.requires_key,
            }
        )
    return {"providers": items}


@router.get("/settings")
async def settings(request: Request) -> dict[str, Any]:
    config = request.app.state.config
    prompts = request.app.state.prompts
    profiles = await prompts.list_profiles()
    return {
        "version": __version__,
        "default_model": config.gateway.default_model,
        "models": [model.model_dump() for model in config.available_models()],
        "prompt_profiles": profiles,
        "default_prompt_profile": config.prompts.default_profile,
        "search": request.app.state.search.status(),
        "memory": request.app.state.memory.status().as_dict(),
        "privacy": {
            "local_archive": True,
            "api_keys": "environment-only; never returned by this API or logged",
            "search_payload": "current query only",
        },
    }


# ----- prompts ----------------------------------------------------------
@router.get("/prompts/profiles")
async def prompt_profiles(request: Request) -> dict[str, Any]:
    return {"profiles": await request.app.state.prompts.list_profiles()}


@router.post("/prompts/profiles", status_code=201)
async def create_prompt_profile(request: Request, body: ProfileCreate) -> dict[str, Any]:
    profile = await request.app.state.prompts.create_profile(
        name=body.name,
        description=body.description,
        system_template=body.system_template,
    )
    return {"profile": profile}


@router.get("/prompts/profiles/{profile_id}")
async def get_prompt_profile(request: Request, profile_id: str) -> dict[str, Any]:
    return {"profile": await request.app.state.prompts.get_profile(profile_id)}


@router.put("/prompts/profiles/{profile_id}")
async def update_prompt_profile(
    request: Request, profile_id: str, body: ProfileUpdate
) -> dict[str, Any]:
    profile = await request.app.state.prompts.update_profile(
        profile_id,
        system_template=body.system_template,
        change_note=body.change_note,
    )
    return {"profile": profile}


@router.post("/prompts/profiles/{profile_id}/activate")
async def activate_prompt_version(
    request: Request, profile_id: str, body: VersionActivate
) -> dict[str, Any]:
    profile = await request.app.state.prompts.activate_version(profile_id, body.version)
    return {"profile": profile}


@router.post("/prompts/profiles/{profile_id}/reset")
async def reset_prompt_profile(request: Request, profile_id: str) -> dict[str, Any]:
    profile = await request.app.state.prompts.reset_profile(profile_id)
    return {"profile": profile}


@router.delete("/prompts/profiles/{profile_id}", status_code=204)
async def delete_prompt_profile(request: Request, profile_id: str) -> Response:
    await request.app.state.prompts.delete_profile(profile_id)
    return Response(status_code=204)


@router.post("/prompts/preview")
async def preview_prompt(request: Request, body: PromptPreview) -> dict[str, Any]:
    manager: PromptManager = request.app.state.prompts
    rendered = manager.preview(body.system_template, body.variables)
    return {"rendered": rendered}


# ----- health / reserved extensions ------------------------------------
@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    config = request.app.state.config
    memory = request.app.state.memory.status().as_dict()
    available = sum(1 for model in config.available_models() if model.available)
    return {
        "status": "ok",
        "version": __version__,
        "time": iso_now(),
        "archive": {"type": "sqlite", "local": True, "formal_memory_path": True},
        "models": {"total": len(config.models), "configured": available},
        "memory": memory,
        "search": request.app.state.search.status(),
    }


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/memory/status")
async def memory_status(request: Request) -> dict[str, Any]:
    memory = request.app.state.memory
    result = memory.status().as_dict()
    public_url = getattr(memory, "public_base_url", None)
    if callable(public_url):
        result["base_url"] = public_url()
    result["contract"] = {
        "version": request.app.state.config.memory.contract_version,
        "status": "Reserved for future use",
        "default_enabled": False,
    }
    return {"memory": result}


@router.post("/memory/probe")
async def memory_probe(request: Request, body: MemoryProbe) -> dict[str, Any]:
    result = await request.app.state.memory.health(force=body.force)
    return {"memory": result.as_dict()}


@router.get("/search/status")
async def search_status(request: Request) -> dict[str, Any]:
    return {"search": request.app.state.search.status()}
