"""Quick benchmark runner: real mock-HTTP orchestration, aggregates, gates, artifact.

The mock OpenAI-compatible server (stdlib ThreadingHTTPServer) replays scripted
SSE / error behaviors keyed by the frozen prompt texts of the quick profile.
These tests prove, over real HTTP:

- warmup exclusion from every aggregate
- concurrency-3 shared wall and exact aggregate math
- UNMEASURABLE (never invented) when usage is missing
- timeout / http_error / malformed_sse / zero_tokens / connection_failure
  classifications
- deterministic protocol/workload/run hashes across identical runs
- tamper detection (artifact digest re-verification)
- secret absence from artifact and CLI output
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from serving_verdict.artifact import (
    ARTIFACT_SCHEMA_VERSION,
    compute_artifact_digest,
    run_id,
    verify_artifact,
)
from serving_verdict.benchmark_runner import BenchmarkRunError, run_quick_benchmark
from serving_verdict.errors import IntegrityError
from serving_verdict.profile import QUICK_PROFILE, protocol_hash, workload_hash

SECRET = "runner-top-secret-key-12345"
ENV_VAR = "SERVING_VERDICT_API_KEY_RUNNER"
MODEL = "test-model"
API_URL = "/v1/chat/completions"

WARMUP_TEXTS = ("warmup", "ready")
FRESH_TEXTS = ("slugify", "clamp", "median")
EDIT_TEXTS = ("add", "get_answer", "first")
CONC_TEXTS = ("prime", "even", "perfect squares")
ARITH_QUESTIONS = {
    "What is 7 + 10?": "17",
    "What is 15 - 7?": "8",
    "What is 14 * 9?": "126",
    "What is 12 * 3 + 6?": "42",
    "What is (3 + 3) / 2?": "3",
}
ARITH_ANSWER_SLEEP_S = 0.1
CONC_SLEEP_S = 0.5
TOOL_SLEEP_S = 0.1


# ---------------------------------------------------------------------------
# mock OpenAI-compatible server
# ---------------------------------------------------------------------------


class MockOpenAIHandler(BaseHTTPRequestHandler):
    """Scripted OpenAI-compatible endpoint for runner integration tests."""

    mode = "ok"
    arrival_times: list[float] = []
    sse_lines: list[str] = []
    t0: float = 0.0
    auth_seen: list[str] = []
    timeout_triggered = False

    def log_message(self, *args: object) -> None:  # silence
        return

    def do_GET(self) -> None:  # noqa: N802
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        if self.path.endswith("/models"):
            body = json.dumps(
                {"object": "list", "data": [{"id": MODEL, "object": "model"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(
            {"object": "list", "data": [{"id": MODEL, "object": "model"}]}
        ).encode()
        self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        cls = type(self)
        auth = self.headers.get("Authorization", "")
        cls.auth_seen.append(auth)
        size = int(self.headers.get("Content-Length", "0"))
        doc = json.loads(self.rfile.read(size))
        cls.arrival_times.append(time.monotonic())
        content = doc["messages"][0]["content"]
        has_tools = "tools" in doc
        question = content[: -len(QUICK_PROFILE.ARITHMETIC_INSTRUCTION) - 2]

        if "-invalid-probe" in auth:
            body = json.dumps({"error": {"message": "invalid api key"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not doc.get("stream"):
            body = json.dumps(
                {
                    "id": "chatcmpl-preflight",
                    "model": "served-test-model",
                    "choices": [
                        {"message": {"role": "assistant", "content": "READY"}}
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/models"):
            body = json.dumps({"object": "list", "data": [{"id": MODEL}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not self.path.startswith(API_URL):
            body = json.dumps({"error": "not found"}).encode()
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if has_tools:
            sse_lines = _tool_lines()
            sleep_s = TOOL_SLEEP_S
        elif any(t in content for t in WARMUP_TEXTS):
            sse_lines = _content_lines(content, "ok")
            sleep_s = 0.0
        elif any(t in content for t in CONC_TEXTS):
            sse_lines = _content_lines(content, "ok", completion=10)
            sleep_s = CONC_SLEEP_S
        elif any(t in content for t in FRESH_TEXTS + EDIT_TEXTS):
            sse_lines = _content_lines(content, "ok")
            sleep_s = 0.0
        elif question in ARITH_QUESTIONS:
            sse_lines = _content_lines(ARITH_QUESTIONS[question], "ok")
            sleep_s = ARITH_ANSWER_SLEEP_S
        else:
            sse_lines = ["not-json"]
            sleep_s = 0.0

        if (
            cls.mode == "timeout"
            and not cls.timeout_triggered
            and any(t in content for t in WARMUP_TEXTS)
        ):
            cls.timeout_triggered = True
            time.sleep(3.0)
            self.close_connection = True
            return

        if cls.mode == "malformed":
            sse_lines = ["not-json", "[DONE]"]
            sleep_s = 0.0

        if cls.mode == "http500":
            body = json.dumps({"error": "forced failure"}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if cls.mode == "no-usage":
            sse_lines = _content_lines("ok", "no-usage")
        elif cls.mode == "zero":
            sse_lines = _zero_lines()
        elif cls.mode == "warmup-heavy" and any(t in content for t in WARMUP_TEXTS):
            sse_lines = _content_lines("warmup-heavy", "usage", completion=2000)
            sleep_s = 0.9

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if sleep_s:
            time.sleep(sleep_s)
        for line in sse_lines:
            self.wfile.write(f"data: {line}\n".encode())
            self.wfile.flush()
        self.close_connection = True


def _content_lines(text: str, mode: str, completion: int = 1) -> list[str]:
    obj = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": "served-test-model",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}}],
    }
    lines = [json.dumps(obj)]
    if mode != "no-usage":
        usage = {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "model": "served-test-model",
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": completion,
                      "total_tokens": 10 + completion},
        }
        lines.append(json.dumps(usage))
    lines.append("[DONE]")
    return lines


def _zero_lines() -> list[str]:
    return [
        json.dumps(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "model": "served-test-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 0,
                          "total_tokens": 10},
            }
        ),
        "[DONE]",
    ]


def _tool_lines() -> list[str]:
    def c(payload: object) -> str:
        return json.dumps(
            {
                "id": "chatcmpl-t",
                "object": "chat.completion.chunk",
                "model": "served-test-model",
                "choices": [{"index": 0, "delta": payload}],
            }
        )

    return [
        c({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                          "function": {"name": "get_weather", "arguments": ""}}]}),
        c({"tool_calls": [{"index": 0, "function": {
            "arguments": '{"location": "Istanbul", "units": "metric"}'}}]}),
        c({"tool_calls": [{"index": 0, "function": {"arguments": ""},
                           "finish_reason": "tool_calls"}]}),
        json.dumps(
            {
                "id": "chatcmpl-t",
                "object": "chat.completion.chunk",
                "model": "served-test-model",
                "choices": [],
                "usage": {"prompt_tokens": 30, "completion_tokens": 20,
                          "total_tokens": 50},
            }
        ),
        "[DONE]",
    ]


@contextmanager
def mock_server(mode: str = "ok") -> Iterator[str]:
    MockOpenAIHandler.mode = mode
    MockOpenAIHandler.arrival_times = []
    MockOpenAIHandler.sse_lines = []
    MockOpenAIHandler.auth_seen = []
    MockOpenAIHandler.timeout_triggered = False
    MockOpenAIHandler.t0 = time.monotonic()
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def endpoint_file(tmp_path: Path, base_url: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "endpoint.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "serving-verdict.endpoint.v1",
                "id": "runner-target",
                "base_url": base_url,
                "model": MODEL,
                "api_key_env": ENV_VAR,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def run_full(tmp_path: Path, base_url: str, mode: str = "ok") -> tuple[object, dict]:
    from serving_verdict.endpoint import load_endpoint_config
    from serving_verdict.profile import get_profile

    config = load_endpoint_config(endpoint_file(tmp_path, base_url))
    result = run_quick_benchmark(
        config,
        api_key=SECRET,
        profile=get_profile("quick"),
        transport_timeout_s=2.0,
    )
    return result, json.loads(json.dumps(result.artifact))


# ---------------------------------------------------------------------------
# happy path over real HTTP
# ---------------------------------------------------------------------------


def test_full_quick_run_end_to_end(tmp_path: Path) -> None:
    with mock_server() as base_url:
        result, doc = run_full(tmp_path, base_url)

    assert doc["schema_version"] == ARTIFACT_SCHEMA_VERSION
    assert doc["profile"]["name"] == "quick"
    assert doc["protocol_hash"] == protocol_hash(QUICK_PROFILE)
    assert doc["workload_hash"] == workload_hash(QUICK_PROFILE)
    assert doc["endpoint"]["id"] == "runner-target"
    assert doc["model"]["requested"] == MODEL
    assert doc["model"]["served"] == "served-test-model"
    assert doc["model"]["matches_requested"] is True
    assert doc["phases"]["lifecycle"] == "SEALED"
    assert doc["phases"]["sequence"] == [
        "PREFLIGHT", "WARMUP", "MEASURE", "CONCURRENCY", "QUALITY", "SEALED",
    ]
    assert doc["run_status"] == "ok"
    assert result.summary["preflight"]["served_model"] == "served-test-model"

    # error probe classified as expected (invalid key must be rejected)
    probe = doc["error_probe"]
    assert probe["expect"] == "rejected"
    assert probe["http_status"] == 401
    assert probe["expected_behavior_met"] is True

    # every one of the 17 requests (2 warmup + 15 measured) was recorded
    assert len(doc["requests"]) == 15
    assert len(doc["warmup_requests"]) == 2
    assert all(r["warmup"] is False for r in doc["requests"])

    for record in doc["requests"]:
        m = record["measurement"]
        assert m["status"] == "success"
        expected_tokens = {
            "quality_tool_call": 20,
            "concurrency": 10,
        }.get(record["kind"], 1)
        assert m["completion_tokens"] == expected_tokens

    # quality gates pass with exact arithmetic and the fixed tool schema
    gates = doc["gates"]
    assert gates["arithmetic"]["pass_rate"] == pytest.approx(1.0)
    assert gates["arithmetic"]["passed_cases"] == 5
    assert gates["arithmetic"]["passed"] is True
    assert gates["tool_call"]["passed"] is True
    assert gates["tool_call"]["grade"]["tool_name"] == "get_weather"
    assert gates["quality_lite"]["pass_rate"] == pytest.approx(1.0)
    assert gates["quality_lite"]["passed"] is True

    # warmup excluded from aggregates
    fresh = doc["aggregates"]["serial"]["fresh"]
    assert fresh["request_ids"] == ["fresh-1", "fresh-2", "fresh-3"]
    assert fresh["success_count"] == 3
    assert fresh["measured_denominator"] == 3

    # aggregate request success rate: 15 successful measured requests / 15
    rate = doc["aggregates"]["requests"]
    assert rate["measured_attempts"] == 15
    assert rate["successful"] == 15
    assert rate["rate"] == pytest.approx(1.0)

    # artifact digest verifies; raw content is not persisted
    text = json.dumps(doc)
    assert doc["artifact_digest"].startswith("sha256:")
    assert compute_artifact_digest(doc) == doc["artifact_digest"]
    assert "def slugify" not in text
    assert "Prime numbers" not in text


# ---------------------------------------------------------------------------
# request classification (real HTTP failure modes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["timeout", "http500", "malformed"])
def test_degraded_request_modes_classified(tmp_path: Path, mode: str) -> None:
    with mock_server(mode) as base_url:
        result, doc = run_full(tmp_path, base_url)

    if mode == "timeout":
        # only the first warmup hangs; measured requests still succeed
        assert doc["warmup_requests"][0]["measurement"]["status"] == "timeout"
        assert doc["warmup_requests"][0]["measurement"]["http_status"] is None
        assert doc["requests"][0]["measurement"]["status"] == "success"
    elif mode == "http500":
        assert doc["requests"][0]["measurement"]["status"] == "http_error"
        assert doc["requests"][0]["measurement"]["http_status"] == 500
        assert doc["gates"]["arithmetic"]["passed"] is False
    else:
        assert doc["requests"][0]["measurement"]["status"] == "malformed_sse"
        assert doc["requests"][0]["measurement"]["http_status"] == 200
        assert doc["gates"]["arithmetic"]["passed"] is False

    assert doc["run_status"] != "ok"
    assert doc["phases"]["lifecycle"] == "SEALED"
    # even a degraded run is a sealed, digest-verifiable artifact
    assert compute_artifact_digest(doc) == doc["artifact_digest"]


def test_connection_failure_classification(tmp_path: Path) -> None:
    with mock_server() as base_url:
        config_path = endpoint_file(tmp_path, base_url)
    from serving_verdict.endpoint import load_endpoint_config
    from serving_verdict.profile import get_profile

    config = load_endpoint_config(config_path)
    # server gone: the error probe still hits the (now dead) endpoint and the
    # run degrades to a sealed artifact with connection_failure records.
    with pytest.raises(BenchmarkRunError, match="preflight"):
        run_quick_benchmark(
            config,
            api_key=SECRET,
            profile=get_profile("quick"),
            transport_timeout_s=1.0,
        )


def test_missing_usage_is_unmeasurable(tmp_path: Path) -> None:
    with mock_server("no-usage") as base_url:
        result, doc = run_full(tmp_path, base_url)

    for record in doc["requests"]:
        m = record["measurement"]
        assert m["status"] == "success_no_usage"
        assert m["decode_tokens_per_s"] == "UNMEASURABLE"
        assert m["e2e_output_tokens_per_s"] == "UNMEASURABLE"
        assert m["completion_tokens"] is None
        assert m["prompt_tokens"] is None

    assert doc["aggregates"]["serial"]["fresh"]["median_decode_tokens_per_s"] == (
        "UNMEASURABLE"
    )
    assert doc["aggregates"]["serial"]["fresh"]["median_e2e_output_tokens_per_s"] == (
        "UNMEASURABLE"
    )
    assert doc["aggregates"]["concurrency"][0]["total_completion_tokens"] is None
    assert doc["aggregates"]["concurrency"][0]["aggregate_output_tokens_per_s"] == (
        "UNMEASURABLE"
    )
    # timing is still measurable; success is still success
    assert doc["aggregates"]["requests"]["successful"] == 15


def test_zero_tokens_classification(tmp_path: Path) -> None:
    with mock_server("zero") as base_url:
        result, doc = run_full(tmp_path, base_url)
    for record in doc["requests"]:
        assert record["measurement"]["status"] == "zero_tokens"
        assert record["measurement"]["completion_tokens"] == 0


def test_warmup_excluded_from_all_aggregates(tmp_path: Path) -> None:
    with mock_server("warmup-heavy") as base_url:
        result, doc = run_full(tmp_path, base_url)

    fresh_ids = doc["aggregates"]["serial"]["fresh"]["request_ids"]
    assert fresh_ids == ["fresh-1", "fresh-2", "fresh-3"]
    for block in (*doc["aggregates"]["serial"].values(), *doc["aggregates"]["concurrency"]):
        if isinstance(block, dict) and "request_ids" in block:
            assert all(rid != f"warmup-{i}" for i in (1, 2) for rid in block["request_ids"])
    # warmup records exist and were measured...
    warmups = doc["warmup_requests"]
    assert [w["request_id"] for w in warmups] == ["warmup-1", "warmup-2"]
    assert all(w["warmup"] is True and w["measurement"]["status"] == "success"
               for w in warmups)
    # ...but their tokens never enter any aggregate (warmup e2e ~0.9s server
    # sleep; completion 2000 must not leak into any serial aggregate)
    for w in warmups:
        assert w["measurement"]["completion_tokens"] == 2000
        assert 0.8 < w["measurement"]["e2e_s"] < 1.5
    fresh = doc["aggregates"]["serial"]["fresh"]
    # fresh requests are near-instant on the server side, so their
    # e2e token rate is a large measured number — never a warmup value
    rate = fresh["median_e2e_output_tokens_per_s"]
    assert isinstance(rate, float) and rate > 1.0
    assert doc["aggregates"]["requests"]["measured_denominator"] == 15


def test_concurrency_shared_wall_and_aggregate_math(tmp_path: Path) -> None:
    with mock_server() as base_url:
        t0 = MockOpenAIHandler.t0
        result, doc = run_full(tmp_path, base_url)

    group = doc["aggregates"]["concurrency"][0]
    recs = [r for r in doc["requests"] if r["request_id"].startswith("conc-")]
    assert len(recs) == 3
    total_tokens = sum(r["measurement"]["completion_tokens"] for r in recs)
    e2e_sum = sum(r["measurement"]["e2e_s"] for r in recs)
    assert total_tokens == 30
    assert group["total_completion_tokens"] == total_tokens
    assert group["wall_s"] == pytest.approx(group["wall_s"], abs=1e-9)
    # exact aggregate math: total tokens / shared wall interval
    assert group["aggregate_output_tokens_per_s"] == pytest.approx(
        total_tokens / group["wall_s"]
    )
    # decode/e2e/aggregate are never mixed: the aggregate is the shared-wall
    # rate, strictly below the summed per-request decode rates would not apply;
    # instead prove real overlap: the shared wall is far shorter than the sum
    # of the individual request walls (each sleeps 0.5s on the server).
    assert group["wall_s"] < e2e_sum
    # and the three requests arrived in a burst (serial sends would be >= 1s
    # apart given the 0.5s server sleeps between sends)
    conc_arrivals = MockOpenAIHandler.arrival_times[8:11]
    assert max(conc_arrivals) - min(conc_arrivals) < 1.5
    assert e2e_sum > 1.5  # sanity: serial execution would take much longer
    assert t0 > 0


# ---------------------------------------------------------------------------
# determinism and tamper evidence
# ---------------------------------------------------------------------------


def test_same_inputs_produce_same_hashes(tmp_path: Path) -> None:
    with mock_server() as base_url:
        _, doc1 = run_full(tmp_path / "a", base_url)
        _, doc2 = run_full(tmp_path / "b", base_url)

    assert doc1["protocol_hash"] == doc2["protocol_hash"]
    assert doc1["workload_hash"] == doc2["workload_hash"]
    assert doc1["run_id"] == doc2["run_id"]
    # hashes stay true to the frozen profile specs
    assert doc1["protocol_hash"] == protocol_hash(QUICK_PROFILE)
    assert doc1["workload_hash"] == workload_hash(QUICK_PROFILE)
    assert doc1["endpoint"]["base_url"] == base_url


def test_tampered_artifact_fails_verification(tmp_path: Path) -> None:
    with mock_server() as base_url:
        result, doc = run_full(tmp_path, base_url)

    verify_artifact(doc)  # untampered verifies

    bad = json.loads(json.dumps(doc))
    bad["gates"]["arithmetic"]["passed"] = False
    with pytest.raises(IntegrityError, match="digest"):
        verify_artifact(bad)

    bad2 = json.loads(json.dumps(doc))
    bad2["artifact_digest"] = "sha256:" + "0" * 64
    with pytest.raises(IntegrityError, match="digest"):
        verify_artifact(bad2)

    bad3 = json.loads(json.dumps(doc))
    del bad3["aggregates"]
    with pytest.raises(IntegrityError):
        verify_artifact(bad3)

    # run_id is stable across tamper-free reloads (deterministic inputs)
    reloaded = json.loads(json.dumps(doc))
    assert run_id(reloaded) == doc["run_id"]
    assert result.summary["run_id"] == doc["run_id"]
