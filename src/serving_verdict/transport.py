"""Credential-safe streaming transport for OpenAI-compatible benchmark requests."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from serving_verdict.endpoint import EndpointConfig

_MAX_LINE_BYTES = 1024 * 1024


class EndpointTransportError(RuntimeError):
    """A request could not establish or safely consume the endpoint stream."""


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
        raise EndpointTransportError("endpoint redirect rejected")


def _stream_lines(response: Any) -> Iterator[str]:
    try:
        with response:
            while True:
                raw = response.readline(_MAX_LINE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > _MAX_LINE_BYTES:
                    raise EndpointTransportError("endpoint stream line exceeds size limit")
                try:
                    yield raw.decode("utf-8").rstrip("\r\n")
                except UnicodeDecodeError as exc:
                    raise EndpointTransportError("endpoint stream is not UTF-8") from exc
    except EndpointTransportError:
        raise
    except TimeoutError:
        raise EndpointTransportError("endpoint stream timeout") from None
    except OSError:
        raise EndpointTransportError("endpoint stream connection failure") from None


def stream_chat_completions(
    config: EndpointConfig,
    api_key: str,
    payload: dict[str, object],
    *,
    request_timeout_s: float,
) -> tuple[int, Iterator[str]]:
    """POST one fixed chat-completions request and return status + SSE lines.

    Redirects are rejected before the bearer key can leave the configured
    endpoint. Non-2xx responses return an empty line iterator; their bodies are
    deliberately never read or exposed. Credentials are accepted only at the
    call boundary and are not retained in the returned objects.
    """
    if request_timeout_s <= 0:
        raise EndpointTransportError("request timeout must be positive")
    if not isinstance(api_key, str) or not api_key:
        raise EndpointTransportError("endpoint API key is unavailable")
    body = dict(payload)
    body["stream"] = True
    data = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode()
    request = urllib.request.Request(
        f"{config.base_url}/chat/completions",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        response = opener.open(request, timeout=request_timeout_s)
    except EndpointTransportError:
        raise
    except urllib.error.HTTPError as exc:
        # Never read the remote error body: callers need only the status for
        # deterministic classification.
        exc.close()
        return int(exc.code), iter(())
    except TimeoutError:
        raise EndpointTransportError("endpoint request timeout") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise EndpointTransportError("endpoint request timeout") from None
        raise EndpointTransportError("endpoint connection failure") from None
    except OSError:
        raise EndpointTransportError("endpoint connection failure") from None
    status = int(getattr(response, "status", response.getcode()))
    if not 200 <= status < 300:
        response.close()
        return status, iter(())
    return status, _stream_lines(response)
