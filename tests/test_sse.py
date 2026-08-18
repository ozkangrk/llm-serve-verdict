"""Streaming SSE measurement contracts: TTFT/decode/e2e from API usage only.

Timings are driven by an injected clock so tests are deterministic and never
depend on wall-clock jitter. The clock is sampled at: stream start, every
content delta, and DONE. Token counts come exclusively from the API's
``usage`` object; character-based estimates are forbidden.
"""
from __future__ import annotations

import json

import pytest

from serving_verdict.sse import (
    UNMEASURABLE,
    StreamMeasurement,
    measure_sse,
)


class ScriptedClock:
    """Yields scripted values in order; raises when exhausted (no padding)."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)

    def __call__(self) -> float:
        if not self._values:
            raise AssertionError("clock exhausted: unexpected extra sample")
        return self._values.pop(0)


def chunk(**kwargs: object) -> str:
    base: dict[str, object] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": "test-model",
        "choices": [],
    }
    base.update(kwargs)
    return "data: " + json.dumps(base) + "\n"


def chunk_no_choices() -> str:
    """A chunk with the ``choices`` key genuinely absent (protocol violation)."""
    doc = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": "test-model",
    }
    return "data: " + json.dumps(doc) + "\n"


def content_delta(text: str) -> str:
    return chunk(choices=[{"index": 0, "delta": {"role": "assistant", "content": text}}])


def usage_chunk(prompt_tokens: int, completion_tokens: int) -> str:
    return chunk(
        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )


def done() -> str:
    return "data: [DONE]\n"


def sse_lines(*parts: str) -> list[str]:
    """Join SSE parts (each usually ending in a newline) into raw lines."""
    return ("".join(parts) + "\n").splitlines()


def test_happy_path_ttft_decode_e2e_from_usage_only() -> None:
    lines = sse_lines(
        content_delta("Hel"),
        content_delta("lo"),
        content_delta(" world"),
        usage_chunk(11, 10),
        done(),
    )
    # samples: start, delta1, delta2, delta3, done
    clock = ScriptedClock([0.0, 0.5, 1.5, 2.5, 3.5])
    m = measure_sse(iter(lines), clock, http_status=200)
    assert m.status == "success"
    assert m.prompt_tokens == 11
    assert m.completion_tokens == 10
    assert m.ttft_s == 0.5
    assert m.e2e_s == 3.5
    # decode = 10 tokens / (3.5 - 0.5) = 3.3333 tok/s
    assert m.decode_tokens_per_s == pytest.approx(10.0 / 3.0)
    # e2e = 10 / 3.5
    assert m.e2e_output_tokens_per_s == pytest.approx(10.0 / 3.5)
    assert m.finish_reason == "stop"
    assert m.content == "Hello world"


def test_missing_usage_is_unmeasurable_not_estimated() -> None:
    lines = sse_lines(
        content_delta("Hel"),
        content_delta("lo"),
        done(),
    )
    clock = ScriptedClock([0.0, 0.25, 0.75, 1.0])
    m = measure_sse(iter(lines), clock, http_status=200)
    # streamed content but no usage: no token counts may be invented
    assert m.status == "success_no_usage"
    assert m.completion_tokens is None
    assert m.prompt_tokens is None
    assert m.ttft_s == 0.25  # timing is still measured (stream was valid)
    assert m.e2e_s == 1.0
    assert m.decode_tokens_per_s is UNMEASURABLE
    assert m.e2e_output_tokens_per_s is UNMEASURABLE


def test_zero_tokens_classification() -> None:
    lines = sse_lines(
        usage_chunk(5, 0),
        done(),
    )
    clock = ScriptedClock([0.0, 0.4])
    m = measure_sse(iter(lines), clock, http_status=200)
    assert m.status == "zero_tokens"
    assert m.completion_tokens == 0
    assert m.ttft_s is None  # no generated content delta ever arrived
    assert m.e2e_s == 0.4
    assert m.decode_tokens_per_s is UNMEASURABLE
    assert m.e2e_output_tokens_per_s is UNMEASURABLE


def test_empty_stream_is_malformed_sse() -> None:
    m = measure_sse(iter([]), ScriptedClock([0.0, 0.1]), http_status=200)
    assert m.status == "malformed_sse"
    assert m.content == ""


def test_non_json_data_is_malformed_sse() -> None:
    lines = sse_lines("data: {not json}\n", done())
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1]), http_status=200)
    assert m.status == "malformed_sse"


def test_json_non_object_is_malformed_sse() -> None:
    lines = sse_lines("data: [1, 2, 3]\n", done())
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1]), http_status=200)
    assert m.status == "malformed_sse"


def test_empty_data_payload_is_malformed_sse() -> None:
    # A bare ``data:`` line is an empty event payload -> protocol violation.
    lines = "data:\n\ndata: [DONE]\n".splitlines()
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1]), http_status=200)
    assert m.status == "malformed_sse"


def test_missing_choices_key_is_malformed_sse() -> None:
    lines = sse_lines(chunk_no_choices(), done())
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1]), http_status=200)
    assert m.status == "malformed_sse"


def test_missing_delta_is_malformed_sse() -> None:
    lines = sse_lines(chunk(choices=[{"index": 0}]), done())
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1]), http_status=200)
    assert m.status == "malformed_sse"


def test_empty_choices_array_is_legal_usage_chunk() -> None:
    # Real OpenAI streams: the final usage chunk carries ``choices: []``.
    lines = sse_lines(
        content_delta("a"),
        chunk(choices=[], usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}),
        done(),
    )
    clock = ScriptedClock([0.0, 0.1, 0.5])
    m = measure_sse(iter(lines), clock, http_status=200)
    assert m.status == "success"
    assert m.completion_tokens == 1
    assert m.content == "a"


def test_non_string_content_delta_is_malformed_sse() -> None:
    lines = sse_lines(
        chunk(choices=[{"index": 0, "delta": {"content": 42}}]),
        done(),
    )
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1]), http_status=200)
    assert m.status == "malformed_sse"


def test_http_error_classification() -> None:
    lines = sse_lines(content_delta("x"), done())
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.5, 1.0]), http_status=500)
    assert m.status == "http_error"
    assert m.http_status == 500
    # no metrics are reported from an errored response
    assert m.ttft_s is None
    assert m.e2e_s is None
    assert m.completion_tokens is None


def test_data_after_done_is_malformed_sse() -> None:
    lines = sse_lines(content_delta("a"), done(), content_delta("b"), done())
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1, 0.2, 0.3]), http_status=200)
    assert m.status == "malformed_sse"


def test_comments_and_field_lines_are_sse_compliant() -> None:
    # comments and event:/id: lines are ignored; payload is valid
    obj = json.dumps(
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "model": "m",
            "choices": [{"index": 0, "delta": {"content": "ok"}}],
        }
    )
    raw = ": keepalive comment\nevent: message\nid: 7\ndata: " + obj + "\n\ndata: [DONE]\n"
    lines = raw.splitlines()
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1, 0.2]), http_status=200)
    assert m.status == "success_no_usage"
    assert m.content == "ok"
    # an empty data field is still a (malformed) event, not silently dropped
    lines2 = "data:\n\ndata: [DONE]\n".splitlines()
    m2 = measure_sse(iter(lines2), ScriptedClock([0.0, 0.1]), http_status=200)
    assert m2.status == "malformed_sse"


def test_invalid_usage_types_are_malformed_sse() -> None:
    # bool completion_tokens is a protocol violation, not silently coerced
    lines = sse_lines(
        content_delta("a"),
        chunk(
            choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
            usage={"prompt_tokens": 3, "completion_tokens": True, "total_tokens": 4},
        ),
        done(),
    )
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1, 0.2]), http_status=200)
    assert m.status == "malformed_sse"


def test_usage_float_tokens_rejected() -> None:
    lines = sse_lines(
        content_delta("a"),
        chunk(
            choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
            usage={"prompt_tokens": 3.0, "completion_tokens": 4, "total_tokens": 7},
        ),
        done(),
    )
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1, 0.2]), http_status=200)
    assert m.status == "malformed_sse"


def test_usage_total_inconsistency_rejected() -> None:
    lines = sse_lines(
        content_delta("a"),
        chunk(
            choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
            usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 100},
        ),
        done(),
    )
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1, 0.2]), http_status=200)
    assert m.status == "malformed_sse"


def test_tool_call_delta_accumulation() -> None:
    def tc_delta(payload: object) -> str:
        return chunk(choices=[{"index": 0, "delta": {"tool_calls": [payload]}}])

    lines = sse_lines(
        tc_delta(
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": ""},
            }
        ),
        tc_delta({"index": 0, "function": {"arguments": '{"location": '}}),
        tc_delta({"index": 0, "function": {"arguments": '"Istanbul", "units": "metric"}'}}),
        usage_chunk(30, 20),
        done(),
    )
    clock = ScriptedClock([0.0, 0.1, 0.2, 0.3, 1.3])
    m = measure_sse(iter(lines), clock, http_status=200)
    assert m.status == "success"
    assert m.completion_tokens == 20
    assert len(m.tool_calls) == 1
    tc = m.tool_calls[0]
    assert tc["name"] == "get_weather"
    assert tc["arguments"] == '{"location": "Istanbul", "units": "metric"}'
    parsed = json.loads(tc["arguments"])
    assert parsed == {"location": "Istanbul", "units": "metric"}


def test_malformed_tool_call_index_rejected() -> None:
    lines = sse_lines(
        chunk(choices=[{"index": 0, "delta": {"tool_calls": [{"index": "0"}]}}]),
        done(),
    )
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1]), http_status=200)
    assert m.status == "malformed_sse"


def test_no_first_token_no_ttft() -> None:
    lines = sse_lines(
        usage_chunk(5, 1),
        done(),
    )
    clock = ScriptedClock([0.0, 0.9])
    # usage says 1 completion token but no content delta ever arrived:
    # TTFT cannot be measured; the record stays honest.
    m = measure_sse(iter(lines), clock, http_status=200)
    assert m.status == "success"
    assert m.completion_tokens == 1
    assert m.ttft_s is None
    assert m.e2e_s == 0.9
    assert m.decode_tokens_per_s is UNMEASURABLE  # no decodable window
    assert m.e2e_output_tokens_per_s == pytest.approx(1.0 / 0.9)


def test_measurement_records_carry_no_secret_fields() -> None:
    lines = sse_lines(content_delta("hi"), usage_chunk(2, 1), done())
    m = measure_sse(iter(lines), ScriptedClock([0.0, 0.1, 0.5]), http_status=200)
    public = m.public_record(warmup=False)
    text = json.dumps(public)
    assert "api_key" not in text
    assert "authorization" not in text.lower()
    assert "hi" not in public  # raw prompt/response content is not persisted
    assert public["status"] == "success"
    assert public["completion_tokens"] == 1
    assert public["warmup"] is False
    assert isinstance(public, dict)
    assert isinstance(m, StreamMeasurement)
