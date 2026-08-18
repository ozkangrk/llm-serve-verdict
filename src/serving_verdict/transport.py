"""Streaming HTTP transport for OpenAI-compatible chat completions.

This is the only component that touches the network. Contracts:

- Sends the fixed bearer key (passed by the caller, read from the environment
  via :func:`serving_verdict.endpoint.resolve_api_key`); the key is never
  logged, embedded in raised messages, or written to artifacts.
- Rejects ALL redirects (a redirect must never carry the key to another host).
- Classifies timeout / connection failure cleanly; the remote body is never
  included in raised messages.
- For a 2xx response: yields the HTTP status and an iterator of raw SSE lines
  (``readline`` semantics) so callers can measure streaming deterministically.
- For a non-2xx response: returns the status and an EMPTY line iterator; the
  remote error body is deliberately dropped so it can never leak downstream.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import suppress
from typing import Any

from serving_verdict.endpoint import EndpointConfig

CHAT_PATH = "/chat/completions"


class EndpointTransportError(RuntimeError):
    """A streaming chat request failed at the transport layer."""


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


def _line_iterator(response: Any) -> Iterator[str]:
    """Yield decoded lines from a 2xx streaming body (readline semantics)."""
    try:
        while True:
            try:
                line = response.readline()
            except TimeoutError:
                raise EndpointTransportError("endpoint stream timeout") from None
            except (AttributeError, ValueError, OSError):
                raise EndpointTransportError("endpoint stream connection failure") from None
            if line == b"":
                break
            try:
                yield line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                raise EndpointTransportError("endpoint stream is not UTF-8") from exc
    finally:
        response.close()


def stream_chat_completions(
    config: EndpointConfig,
    api_key: str,
    payload: dict[str, Any],
    *,
    request_timeout_s: float,
) -> tuple[int, Iterator[str]]:
    """POST one streaming chat-completions request.

    Returns ``(http_status, lines)``. For 2xx the iterator streams raw SSE
    lines; for non-2xx it is empty (the error body is dropped). Transport
    failures (timeout, connection failure, redirect) raise
    :class:`EndpointTransportError` with a message that carries neither the
    API key nor any remote body content.
    """
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{config.base_url}{CHAT_PATH}",
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
        return response.status, _line_iterator(response)
    except EndpointTransportError:
        raise
    except urllib.error.HTTPError as exc:
        # Drain and discard the error body: it is never surfaced downstream.
        with suppress(TimeoutError, OSError):
            exc.read()
        return exc.code, iter(())
    except TimeoutError:
        raise EndpointTransportError("endpoint request timeout") from None
    except (urllib.error.URLError, OSError) as exc:
        # Fail-closed: the underlying reason (which may embed remote details)
        # is never propagated into the message.
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise EndpointTransportError("endpoint request timeout") from None
        raise EndpointTransportError("endpoint connection failure") from None
