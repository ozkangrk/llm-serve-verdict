"""OpenAI-compatible streaming SSE measurement.

Measurement rules (PRODUCT_V1_SPEC.md):

- Token counts come ONLY from the API ``usage`` object. Character-based
  estimates are forbidden. Missing stream/usage yields ``UNMEASURABLE`` —
  never an invented number.
- ``ttft_s``: request start to the first *generated content* delta.
- ``decode_tokens_per_s``: completion_tokens / (e2e - ttft).
- ``e2e_output_tokens_per_s``: completion_tokens / e2e.
- Every anomaly (timeout, HTTP error, malformed SSE, zero-token completion)
  is classified separately and reported with that classification.

This module is pure parsing/measurement: it consumes an iterator of raw SSE
lines plus an injectable clock, so tests are fully deterministic.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

UNMEASURABLE = "UNMEASURABLE"
SSE_DONE = object()  # sentinel yielded for literal ``data: [DONE]`` events

_MAX_DATA_BYTES = 1024 * 1024  # per-event data size cap (fail-closed)


@dataclass(slots=True)
class StreamMeasurement:
    """One measured stream. Fields are ``None`` when not applicable.

    ``decode_tokens_per_s`` / ``e2e_output_tokens_per_s`` are either a float
    or the sentinel string ``UNMEASURABLE`` (never an estimate).
    """

    status: str  # success | success_no_usage | zero_tokens | http_error | malformed_sse | timeout
    http_status: int | None = None
    ttft_s: float | None = None
    e2e_s: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    decode_tokens_per_s: float | str | None = None
    e2e_output_tokens_per_s: float | str | None = None
    finish_reason: str | None = None
    content: str = ""
    tool_calls: list[dict[str, str]] = field(default_factory=list)

    def public_record(self, *, warmup: bool) -> dict[str, Any]:
        """Canonical per-request record. Contains no credentials or raw secrets."""
        return {
            "status": self.status,
            "http_status": self.http_status,
            "ttft_s": self.ttft_s,
            "e2e_s": self.e2e_s,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "decode_tokens_per_s": self.decode_tokens_per_s,
            "e2e_output_tokens_per_s": self.e2e_output_tokens_per_s,
            "finish_reason": self.finish_reason,
            "content_chars": len(self.content),
            "tool_calls": self.tool_calls,
            "warmup": warmup,
        }


class _SSEMalformed(Exception):
    pass


def _parse_sse_events(lines: Iterator[str]) -> Iterator[Any]:
    """Yield payloads of SSE events, strictly.

    Yields dict payloads for JSON events and the sentinel ``SSE_DONE`` for a
    literal ``data: [DONE]`` event.

    Framing: OpenAI-compatible servers emit one complete ``data:`` event per
    line (events delimited by a single newline, not blank lines). The SSE
    spec, however, allows a single event's data to span multiple ``data:``
    lines separated within the event. Both are supported: when a new
    ``data:`` line arrives and the accumulated payload already parses as a
    complete event, the previous payload is flushed as its own event;
    otherwise the line is appended to the pending payload (multi-line
    data). A blank line always flushes the pending payload (spec behavior).

    Within an event only ``data:`` lines contribute to the payload (joined
    with "\\n"); comment (``:``) and other field lines (event:, id:, retry:)
    are ignored per the SSE spec. After ``data:``/``:`` a single optional
    space is stripped. Any data payload that is not the [DONE] sentinel, not
    a JSON object, or that exceeds the size cap, raises _SSEMalformed.
    """
    data_parts: list[str] = []
    data_seen = False

    def flush() -> Any | None:
        nonlocal data_parts, data_seen
        if not data_seen:
            return None
        payload = _event_payload("\n".join(data_parts))
        data_parts = []
        data_seen = False
        return payload

    def pending_is_complete() -> bool:
        """True iff the pending payload is a complete JSON *object*.

        OpenAI-compatible servers delimit events by single newlines, so we
        must decide when the accumulated data is a finished event. A
        finished chunk is a balanced ``{...}`` object; anything else (or
        unbalanced braces) keeps accumulating. JSON scalars/arrays never
        count as complete events, so they accumulate until the stream ends,
        where they are then rejected as malformed SSE.
        """
        text = "\n".join(data_parts)
        return _object_complete(text)

    for raw in lines:
        line = raw[:-1] if raw.endswith("\r") else raw
        if line == "":
            payload = flush()
            if payload is not None:
                yield payload
            continue
        if line.startswith(":"):
            continue  # comment line
        field_name, sep, value = line.partition(":")
        if not sep:
            # Lines without a colon inside an event are not SSE data; ignore.
            continue
        if value.startswith(" "):
            value = value[1:]
        if field_name == "data":
            if data_seen and data_parts and pending_is_complete():
                # OpenAI framing: previous line already formed a complete
                # event; flush it and start a new one.
                payload = flush()
                if payload is not None:
                    yield payload
            data_parts.append(value)
            data_seen = True
            if sum(len(p) for p in data_parts) > _MAX_DATA_BYTES:
                raise _SSEMalformed("SSE data exceeds size limit")
            continue
        # event:/id:/retry: and unknown fields: ignored by design
    payload = flush()
    if payload is not None:
        yield payload


def _event_payload(text: str) -> Any:
    if text == "[DONE]":
        return SSE_DONE
    return _json_object(text)


def _object_complete(text: str) -> bool:
    """Check whether ``text`` is a brace-balanced JSON object (string-aware).

    This is a syntactic completeness probe (not a full parse): it tracks
    ``{``/``}`` depth outside of string literals, honoring escape sequences.
    Returns False for empty text or any non-object start.
    """
    if not text.startswith("{"):
        return False
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


def _json_object(text: str) -> dict[str, Any]:
    if not text:
        raise _SSEMalformed("empty SSE data payload")
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        reason = exc.msg if isinstance(exc, json.JSONDecodeError) else "invalid data"
        raise _SSEMalformed(f"SSE data is not valid JSON: {reason}") from exc
    if not isinstance(value, dict):
        raise _SSEMalformed("SSE data must be a JSON object")
    return value


def _check_chunk_envelope(chunk: dict[str, Any]) -> None:
    """Strictly validate the OpenAI stream envelope (fail-closed)."""
    choices = chunk.get("choices")
    if choices is None:
        # usage-only chunks may appear without choices on some servers? No:
        # the OpenAI protocol always carries choices; missing = malformed.
        raise _SSEMalformed("chunk missing choices")
    if not isinstance(choices, list):
        raise _SSEMalformed("choices must be a list")
    if not choices and chunk.get("usage") is None:
        raise _SSEMalformed("choices must not be empty without usage")
    for choice in choices:
        if not isinstance(choice, dict):
            raise _SSEMalformed("choice must be an object")
        delta = choice.get("delta")
        if delta is None or not isinstance(delta, dict):
            raise _SSEMalformed("choice missing delta")
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise _SSEMalformed("delta.content must be a string")
        tool_calls = delta.get("tool_calls")
        if tool_calls is not None:
            if not isinstance(tool_calls, list):
                raise _SSEMalformed("delta.tool_calls must be a list")
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    raise _SSEMalformed("tool call delta must be an object")
                fn = tc.get("function")
                if fn is not None and not isinstance(fn, dict):
                    raise _SSEMalformed("tool call function must be an object")
                if fn is not None:
                    args = fn.get("arguments")
                    if args is not None and not isinstance(args, str):
                        raise _SSEMalformed("tool call arguments must be a string")
        finish = choice.get("finish_reason")
        if finish is not None and not isinstance(finish, str):
            raise _SSEMalformed("finish_reason must be a string")
    usage = chunk.get("usage")
    if usage is not None:
        _validate_usage(usage)


def _validate_usage(usage: Any) -> None:
    if not isinstance(usage, dict):
        raise _SSEMalformed("usage must be an object")
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        # bool is an int subclass in Python; reject it explicitly.
        if isinstance(value, bool) or not isinstance(value, int):
            raise _SSEMalformed(f"usage.{key} must be an integer")
        if value < 0:
            raise _SSEMalformed(f"usage.{key} must be non-negative")
    prompt = usage["prompt_tokens"]
    completion = usage["completion_tokens"]
    if usage["total_tokens"] != prompt + completion:
        raise _SSEMalformed("usage.total_tokens is inconsistent")


def _merge_tool_call(target: dict[str, str], delta: dict[str, Any], index: int) -> None:
    if "id" in delta and isinstance(delta["id"], str):
        target["id"] = delta["id"]
    fn = delta.get("function")
    if isinstance(fn, dict):
        if isinstance(fn.get("name"), str):
            target["name"] = fn["name"]
        if isinstance(fn.get("arguments"), str):
            target["arguments"] += fn["arguments"]
    if not target.get("id"):
        target["id"] = f"tc-{index}"
    target.setdefault("name", "")
    target.setdefault("arguments", "")


def measure_sse(
    lines: Iterator[str],
    clock: Callable[[], float],
    *,
    http_status: int | None = None,
) -> StreamMeasurement:
    """Measure one streamed chat-completions response.

    ``clock`` returns the current time in seconds relative to request start.
    It is sampled at: request start, each content delta, and stream end.
    A non-2xx ``http_status`` short-circuits to ``http_error`` (no metrics).
    """
    if http_status is not None and not 200 <= http_status < 300:
        return StreamMeasurement(status="http_error", http_status=http_status)

    t_start = clock()
    ttft: float | None = None
    content_parts: list[str] = []
    tool_calls: dict[int, dict[str, str]] = {}
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    done = False

    try:
        for event in _parse_sse_events(lines):
            if done:
                raise _SSEMalformed("data received after [DONE]")
            if event is SSE_DONE:
                done = True
                break
            if not isinstance(event, dict):  # pragma: no cover - parser guarantees
                raise _SSEMalformed("non-object SSE event")
            chunk: dict[str, Any] = event
            _check_chunk_envelope(chunk)
            for choice in chunk["choices"]:
                delta = choice["delta"]
                content = delta.get("content")
                if isinstance(content, str) and content:
                    now = clock()
                    if ttft is None:
                        ttft = now
                    content_parts.append(content)
                for tc_delta in delta.get("tool_calls", []) or []:
                    index = tc_delta.get("index")
                    if not isinstance(index, int) or isinstance(index, bool):
                        raise _SSEMalformed("tool call index must be an integer")
                    target = tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    _merge_tool_call(target, tc_delta, index)
                if isinstance(choice.get("finish_reason"), str):
                    finish_reason = choice["finish_reason"]
            usage = _extract_usage(chunk)
        if not done:
            raise _SSEMalformed("stream ended without [DONE]")
    except _SSEMalformed:
        return StreamMeasurement(status="malformed_sse", http_status=http_status)

    e2e = clock() - t_start
    content = "".join(content_parts)
    prompt_tokens: int | None
    completion_tokens: int | None
    if usage is None:
        prompt_tokens = None
        completion_tokens = None
    else:
        prompt_tokens = usage["prompt_tokens"]
        completion_tokens = usage["completion_tokens"]

    if usage is None:
        # Timing was measurable; throughput cannot be (no token counts).
        return StreamMeasurement(
            status="success_no_usage",
            http_status=http_status,
            ttft_s=ttft,
            e2e_s=e2e,
            prompt_tokens=None,
            completion_tokens=None,
            decode_tokens_per_s=UNMEASURABLE,
            e2e_output_tokens_per_s=UNMEASURABLE,
            finish_reason=finish_reason,
            content=content,
            tool_calls=[tool_calls[i] for i in sorted(tool_calls)],
        )

    if prompt_tokens is None or completion_tokens is None:
        return StreamMeasurement(status="malformed_sse", http_status=http_status)

    if completion_tokens == 0:
        e2e_s = e2e
        return StreamMeasurement(
            status="zero_tokens",
            http_status=http_status,
            ttft_s=ttft,
            e2e_s=e2e_s,
            prompt_tokens=prompt_tokens,
            completion_tokens=0,
            decode_tokens_per_s=UNMEASURABLE,
            e2e_output_tokens_per_s=UNMEASURABLE,
            finish_reason=finish_reason,
            content=content,
            tool_calls=[tool_calls[i] for i in sorted(tool_calls)],
        )

    decode_window = e2e - (ttft if ttft is not None else 0.0)
    if ttft is None or decode_window <= 0:
        decode: float | str = UNMEASURABLE
    else:
        decode = completion_tokens / decode_window
    e2e_rate: float | str = completion_tokens / e2e if e2e > 0 else UNMEASURABLE

    return StreamMeasurement(
        status="success",
        http_status=http_status,
        ttft_s=ttft,
        e2e_s=e2e,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        decode_tokens_per_s=decode,
        e2e_output_tokens_per_s=e2e_rate,
        finish_reason=finish_reason,
        content=content,
        tool_calls=[tool_calls[i] for i in sorted(tool_calls)],
    )


def _extract_usage(chunk: dict[str, Any]) -> dict[str, int] | None:
    usage = chunk.get("usage")
    if usage is None:
        return None
    _validate_usage(usage)
    return {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
