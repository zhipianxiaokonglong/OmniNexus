"""Connection testing: reachability, auth/models and a real chat probe."""

from __future__ import annotations

import time
from typing import Any

import httpx

from omninexus.errors import ErrorCode, OmniError
from omninexus.providers.urls import (
    BaseUrlError,
    chat_completions_url,
    models_url,
    validate_base_url,
)

_TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ProviderTestResult(dict):
    pass


def _headers(api_key: str | None, headers: dict[str, str]) -> dict[str, str]:
    merged = {
        str(key): str(value)
        for key, value in (headers or {}).items()
        if str(key).lower() not in {"authorization", "x-api-key", "api-key"}
    }
    merged["Content-Type"] = "application/json"
    merged["Accept"] = "application/json"
    if api_key:
        merged["Authorization"] = f"Bearer {api_key}"
    return merged


async def _request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> tuple[httpx.Response | None, int | None, str | None]:
    started = time.monotonic()
    try:
        response = await client.request(method, url, **kwargs)
        return response, int((time.monotonic() - started) * 1000), None
    except httpx.TimeoutException:
        return None, int((time.monotonic() - started) * 1000), "timeout"
    except httpx.HTTPError:
        return None, int((time.monotonic() - started) * 1000), "unreachable"
    except Exception:
        return None, int((time.monotonic() - started) * 1000), "unreachable"


async def test_provider(
    *,
    base_url: str,
    api_key: str | None,
    headers: dict[str, str],
    timeout_seconds: int,
    model: str | None,
    probe_multimodal: bool,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    try:
        base_url = validate_base_url(base_url)
    except BaseUrlError as exc:
        return {
            "status": "unreachable",
            "message": str(exc),
            "levels": [],
            "multimodal": {"status": "not_requested"},
        }
    timeout = max(1.0, min(float(timeout_seconds), 120.0))
    levels: list[dict[str, Any]] = []
    models: list[str] = []
    model_list_supported = False
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
        follow_redirects=False,
        transport=transport,
    ) as client:
        # L1: any HTTP response proves the address is reachable.
        response, elapsed, failure = await _request(
            client, "GET", base_url, headers=_headers(api_key, headers)
        )
        if failure == "timeout":
            levels.append({"level": "reachability", "status": "timeout", "latencyMs": elapsed})
            return {
                "status": "timeout",
                "message": "连接超时，请检查地址和网络。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
            }
        if failure:
            levels.append({"level": "reachability", "status": "unreachable", "latencyMs": elapsed})
            return {
                "status": "unreachable",
                "message": "无法连接，请检查地址和网络。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
            }
        levels.append({"level": "reachability", "status": "ok", "latencyMs": elapsed})

        # L2: /models; auth and optional list.
        response, elapsed, failure = await _request(
            client, "GET", models_url(base_url), headers=_headers(api_key, headers)
        )
        if failure == "timeout":
            levels.append({"level": "models", "status": "timeout", "latencyMs": elapsed})
            return {
                "status": "timeout",
                "message": "模型列表请求超时，请检查网络。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
            }
        if failure:
            levels.append({"level": "models", "status": "unreachable", "latencyMs": elapsed})
            return {
                "status": "unreachable",
                "message": "无法访问 /models，请检查地址。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
            }
        assert response is not None
        if response.status_code in (401, 403):
            levels.append({"level": "models", "status": "auth_failed", "latencyMs": elapsed})
            return {
                "status": "auth_failed",
                "message": "鉴权失败，请检查 API Key。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
            }
        if response.status_code == 200:
            try:
                payload = response.json()
                raw = payload.get("data") if isinstance(payload, dict) else payload
                if isinstance(raw, list):
                    models = [
                        str(item.get("id") or item.get("name"))
                        for item in raw
                        if isinstance(item, dict) and (item.get("id") or item.get("name"))
                    ]
                    model_list_supported = True
                levels.append(
                    {
                        "level": "models",
                        "status": "ok",
                        "latencyMs": elapsed,
                        "modelCount": len(models),
                    }
                )
            except ValueError:
                levels.append({"level": "models", "status": "unverified", "latencyMs": elapsed})
        elif response.status_code in (404, 405):
            levels.append({"level": "models", "status": "unverified", "latencyMs": elapsed})
        else:
            levels.append(
                {
                    "level": "models",
                    "status": "unreachable",
                    "latencyMs": elapsed,
                    "httpStatus": response.status_code,
                }
            )
            return {
                "status": "unreachable",
                "message": f"服务返回 HTTP {response.status_code}，请检查地址/服务。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
            }

        if model and model_list_supported and model not in models:
            levels.append(
                {
                    "level": "model",
                    "status": "model_not_found",
                    "latencyMs": 0,
                    "model": model,
                }
            )
            return {
                "status": "model_not_found",
                "message": "模型不存在于 /models 列表中，请检查模型名。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
            }

        if not model:
            return {
                "status": "unverified",
                "message": "地址与鉴权通过；未指定模型，尚未验证聊天能力。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
                "models": models,
            }

        # L3: a real, minimal chat completion.
        response, elapsed, failure = await _request(
            client,
            "POST",
            chat_completions_url(base_url),
            headers=_headers(api_key, headers),
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            },
        )
        if failure == "timeout":
            levels.append({"level": "model", "status": "timeout", "latencyMs": elapsed})
            return {
                "status": "timeout",
                "message": "模型请求超时，请检查网络或模型状态。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
            }
        if failure:
            levels.append({"level": "model", "status": "unreachable", "latencyMs": elapsed})
            return {
                "status": "unreachable",
                "message": "无法连接聊天接口，请检查 Base URL。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
            }
        assert response is not None
        if response.status_code in (401, 403):
            levels.append({"level": "model", "status": "auth_failed", "latencyMs": elapsed})
            return {
                "status": "auth_failed",
                "message": "鉴权失败，请检查 API Key。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
            }
        if response.status_code == 404:
            levels.append({"level": "model", "status": "model_not_found", "latencyMs": elapsed})
            return {
                "status": "model_not_found",
                "message": "模型不存在，请检查模型名。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
            }
        if response.status_code >= 400:
            levels.append(
                {
                    "level": "model",
                    "status": "unreachable",
                    "latencyMs": elapsed,
                    "httpStatus": response.status_code,
                }
            )
            return {
                "status": "unreachable",
                "message": f"模型测试返回 HTTP {response.status_code}，请检查配置。",
                "levels": levels,
                "multimodal": {"status": "not_requested"},
            }
        levels.append({"level": "model", "status": "ok", "latencyMs": elapsed})

        multimodal = {"status": "not_requested"}
        if probe_multimodal:
            image_response, image_elapsed, image_failure = await _request(
                client,
                "POST",
                chat_completions_url(base_url),
                headers=_headers(api_key, headers),
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Reply OK."},
                                {"type": "image_url", "image_url": {"url": _TINY_PNG}},
                            ],
                        }
                    ],
                    "max_tokens": 4,
                    "stream": False,
                },
            )
            if image_failure:
                multimodal = {
                    "status": "failed",
                    "message": "多模态探测失败，可能不支持图片输入。",
                }
            elif image_response is not None and image_response.status_code < 400:
                multimodal = {"status": "verified", "latencyMs": image_elapsed}
            else:
                multimodal = {
                    "status": "failed",
                    "message": "多模态探测失败，可能不支持图片输入。",
                }

        return {
            "status": "success",
            "message": "连接成功，模型可用。",
            "levels": levels,
            "multimodal": multimodal,
            "models": models,
        }


async def discover_models(
    *,
    base_url: str,
    api_key: str | None,
    headers: dict[str, str],
    timeout_seconds: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    try:
        base_url = validate_base_url(base_url)
    except BaseUrlError as exc:
        raise OmniError(ErrorCode.INVALID_INPUT, str(exc), status_code=422) from exc
    timeout = max(1.0, min(float(timeout_seconds), 120.0))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
        follow_redirects=False,
        transport=transport,
    ) as client:
        response, _, failure = await _request(
            client, "GET", models_url(base_url), headers=_headers(api_key, headers)
        )
    if failure == "timeout":
        raise OmniError(ErrorCode.PROVIDER_TEST_TIMEOUT, "模型列表请求超时。", status_code=504)
    if failure or response is None:
        raise OmniError(ErrorCode.PROVIDER_TEST_UNREACHABLE, "无法访问模型列表。", status_code=502)
    if response.status_code in (401, 403):
        raise OmniError(
            ErrorCode.PROVIDER_TEST_AUTH_FAILED, "鉴权失败，请检查 API Key。", status_code=401
        )
    if response.status_code >= 400:
        raise OmniError(
            ErrorCode.PROVIDER_TEST_UNREACHABLE,
            f"/models 返回 HTTP {response.status_code}；可手动填写模型名。",
            status_code=502,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise OmniError(
            ErrorCode.PROVIDER_TEST_UNREACHABLE,
            "/models 返回非 JSON；可手动填写模型名。",
            status_code=502,
        ) from exc
    raw = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise OmniError(
            ErrorCode.PROVIDER_TEST_UNREACHABLE,
            "/models 返回格式无法识别；可手动填写模型名。",
            status_code=502,
        )
    models = []
    for item in raw:
        if isinstance(item, dict):
            value = item.get("id") or item.get("name")
            if value:
                models.append(str(value))
    return sorted(set(models))
