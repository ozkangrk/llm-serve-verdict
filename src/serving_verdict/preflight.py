"""Fail-closed HTTP readiness probe for OpenAI-compatible endpoints."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from serving_verdict.endpoint import EndpointConfig, resolve_api_key

_MAX_RESPONSE_BYTES = 1024 * 1024


class EndpointPreflightError(RuntimeError):
    """An endpoint could not prove model availability and request readiness."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise EndpointPreflightError("endpoint redirect rejected")


@dataclass(frozen=True, slots=True)
class PreflightResult:
    endpoint_id: str
    requested_model: str
    served_model: str
    models_probe: str
    chat_probe: str
    model_ids: tuple[str, ...]

    def public_payload(self) -> dict[str, object]:
        return {
            "endpoint_id": self.endpoint_id,
            "requested_model": self.requested_model,
            "served_model": self.served_model,
            "models_probe": self.models_probe,
            "chat_probe": self.chat_probe,
            "model_ids": list(self.model_ids),
        }


def _read_json_response(response: Any) -> dict[str, Any]:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > _MAX_RESPONSE_BYTES:
                raise EndpointPreflightError("endpoint response exceeds size limit")
        except ValueError as exc:
            raise EndpointPreflightError("endpoint returned invalid content length") from exc
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise EndpointPreflightError("endpoint response exceeds size limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EndpointPreflightError("endpoint returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise EndpointPreflightError("endpoint JSON response must be an object")
    return value


def _request_json(
    *,
    url: str,
    api_key: str,
    timeout_s: float,
    body: dict[str, object] | None = None,
    allow_not_found: bool = False,
) -> dict[str, Any] | None:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method="GET" if body is None else "POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=timeout_s) as response:
            return _read_json_response(response)
    except EndpointPreflightError:
        raise
    except urllib.error.HTTPError as exc:
        if allow_not_found and exc.code == 404:
            return None
        raise EndpointPreflightError(f"endpoint returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = "timeout" if isinstance(exc, TimeoutError) else "connection failure"
        raise EndpointPreflightError(f"endpoint {reason}") from None


def _model_ids(document: dict[str, Any]) -> tuple[str, ...]:
    data = document.get("data")
    if not isinstance(data, list):
        raise EndpointPreflightError("models response has invalid data")
    ids: list[str] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise EndpointPreflightError("models response contains invalid model entry")
        ids.append(item["id"])
    return tuple(ids)


def _chat_identity(document: dict[str, Any], requested_model: str) -> str:
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise EndpointPreflightError("chat response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise EndpointPreflightError("chat response choice is invalid")
    message = first.get("message")
    if not isinstance(message, dict):
        raise EndpointPreflightError("chat response message is invalid")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise EndpointPreflightError("chat response content is empty")
    served_model = document.get("model", requested_model)
    if not isinstance(served_model, str) or not served_model.strip():
        raise EndpointPreflightError("chat response model identity is invalid")
    return served_model.strip()


def preflight_endpoint(
    config: EndpointConfig, *, timeout_s: float = 10.0
) -> PreflightResult:
    if timeout_s <= 0:
        raise EndpointPreflightError("timeout must be positive")
    api_key = resolve_api_key(config)
    models_document = _request_json(
        url=f"{config.base_url}/models",
        api_key=api_key,
        timeout_s=timeout_s,
        allow_not_found=True,
    )
    if models_document is None:
        models_probe = "unavailable"
        model_ids: tuple[str, ...] = ()
    else:
        model_ids = _model_ids(models_document)
        if config.model not in model_ids:
            raise EndpointPreflightError("requested model is not listed by endpoint")
        models_probe = "matched"
    chat_document = _request_json(
        url=f"{config.base_url}/chat/completions",
        api_key=api_key,
        timeout_s=timeout_s,
        body={
            "model": config.model,
            "messages": [{"role": "user", "content": "Reply exactly: READY"}],
            "temperature": 0,
            "max_tokens": 8,
        },
    )
    if chat_document is None:  # pragma: no cover - POST never permits 404 fallback
        raise EndpointPreflightError("chat endpoint is unavailable")
    served_model = _chat_identity(chat_document, config.model)
    return PreflightResult(
        endpoint_id=config.endpoint_id,
        requested_model=config.model,
        served_model=served_model,
        models_probe=models_probe,
        chat_probe="ready",
        model_ids=model_ids,
    )
