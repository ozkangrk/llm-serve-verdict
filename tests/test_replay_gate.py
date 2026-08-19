"""CI replay regression gate: comparability, thresholds, hard gates (RED first)."""
from __future__ import annotations

import copy
import json

import pytest

from serving_verdict.errors import GateError, IntegrityError
from serving_verdict.replay import RawExecutionResult, run_replay
from serving_verdict.replay_gate import (
    DEFAULT_THRESHOLDS,
    DEFAULT_TOLERANCE,
    GateDecision,
    GateThreshold,
    ReplayGate,
    canonical_digest,
)
from serving_verdict.workload import load_workload
from tests.helpers import make_case, make_jsonl_workload

SECRET = "sk-live-SECRET-abcdef-9f8e7d6c5b4a"
_DEFAULT_USAGE = object()
_DEFAULT_QUALITY = object()


class FakeExecutor:
    def __init__(self, results: dict[str, RawExecutionResult] | None = None) -> None:
        self.results = results or {}

    def execute(
        self, case: object, messages: list[dict[str, object]]
    ) -> RawExecutionResult:
        rid = str(case.request_id)
        return self.results.get(
            rid,
            RawExecutionResult(
                status="success",
                ttft_s=0.5,
                decode_tokens_per_s=40.0,
                e2e_tokens_per_s=38.0,
                usage={"prompt_tokens": 10, "completion_tokens": 100},
                quality={"pass_rate": 1.0},
            ),
        )


def mk_result(
    status: str = "success",
    ttft: float = 0.5,
    decode: float = 40.0,
    e2e: float = 38.0,
    pt: int = 10,
    ct: int = 100,
    quality: dict[str, float] | None | object = _DEFAULT_QUALITY,
    tools: list[dict[str, str]] | None = None,
    usage: dict[str, int] | None | object = _DEFAULT_USAGE,
    error_kind: str | None = None,
    error: str | None = None,
) -> RawExecutionResult:
    doc: dict[str, object] = {
        "status": status,
        "ttft_s": ttft,
        "decode_tokens_per_s": decode,
        "e2e_tokens_per_s": e2e,
        "usage": (
            {"prompt_tokens": pt, "completion_tokens": ct}
            if usage is _DEFAULT_USAGE
            else usage
        ),
        "quality": {"pass_rate": 1.0} if quality is _DEFAULT_QUALITY else quality,
        "error_kind": error_kind,
        "error": error,
    }
    if tools is not None:
        doc["tools"] = tools
    return RawExecutionResult(**doc)  # type: ignore[arg-type]


def run_side(
    tmp_path,
    name: str,
    tag: str,
    cases: list[dict] | None = None,
    seed: int = 1,
    results: dict[str, RawExecutionResult] | None = None,
) -> dict:
    if cases is None:
        cases = [make_case(f"req-{i}", content=f"prompt {i}") for i in range(4)]
    path = tmp_path / f"{name}.jsonl"
    path.write_text(make_jsonl_workload(cases), encoding="utf-8")
    wl = load_workload(path)
    art = run_replay(
        wl,
        FakeExecutor(results),
        executor_tag=tag,
        redaction_policy="none",
        seed=seed,
        sample_size=4,
    )
    return art.to_payload()


BASE = run_side  # alias for readability


class TestGateBasics:
    def test_identical_runs_pass(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        b = BASE(tmp_path, "b", "tag", seed=1)
        res = ReplayGate().evaluate(baseline=a, candidate=b)
        assert res.decision == GateDecision.PASS
        assert res.exit_code == 0
        assert res.digest.startswith("sha256:")
        assert res.reason_codes == ()

    def test_malformed_input_payloads_fail_closed(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag")
        with pytest.raises(GateError):
            ReplayGate().evaluate(baseline="nope", candidate=b"bad")  # type: ignore[arg-type]
        with pytest.raises(GateError):
            ReplayGate().evaluate(baseline={"schema_version": "x"}, candidate=a)

    def test_comparability_mismatch_is_inconclusive(self, tmp_path) -> None:
        big_a = [make_case(f"req-{i}", content=f"prompt {i}") for i in range(4)]
        big_b = [make_case(f"req-{i}", content=f"DIFFERENT {i}") for i in range(4)]
        a = BASE(tmp_path, "a", "tag", cases=big_a)
        b = BASE(tmp_path, "b", "tag", cases=big_b)
        res = ReplayGate().evaluate(baseline=a, candidate=b)
        assert res.decision == GateDecision.INCONCLUSIVE
        assert res.exit_code == 0
        assert "workload_hash_mismatch" in res.reason_codes
        assert res.summary_lines

        # different seed -> different sample -> inconclusive
        a2 = BASE(tmp_path, "a", "tag", seed=1)
        b2 = BASE(tmp_path, "b", "tag", seed=2)
        res2 = ReplayGate().evaluate(baseline=a2, candidate=b2)
        assert res2.decision == GateDecision.INCONCLUSIVE

        # different protocol -> inconclusive
        a3 = BASE(tmp_path, "a", "tag")
        b3 = BASE(tmp_path, "b", "different-tag")
        res3 = ReplayGate().evaluate(baseline=a3, candidate=b3)
        assert res3.decision == GateDecision.INCONCLUSIVE
        assert any(c.startswith("protocol_") for c in res3.reason_codes)

    def test_comparable_across_executor_tags(self, tmp_path) -> None:
        """Workload+sample must match; only the protocol may differ by tag.

        A different executor tag changes the protocol hash, so this run is
        INCONCLUSIVE — the gate compares same workload/sample strictly.
        """
        a = BASE(tmp_path, "a", "tag-a", seed=1)
        b = BASE(tmp_path, "b", "tag-b", seed=1)
        res = ReplayGate().evaluate(baseline=a, candidate=b)
        assert res.decision == GateDecision.INCONCLUSIVE
        assert "protocol_mismatch" in res.reason_codes

    def test_tampered_payload_fails_closed(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        b = BASE(tmp_path, "b", "tag", seed=1)
        tampered = copy.deepcopy(b)
        tampered["entries"][0]["ttft_s"] = 0.1
        with pytest.raises(IntegrityError):
            ReplayGate().evaluate(baseline=a, candidate=tampered)
        tampered2 = copy.deepcopy(b)
        del tampered2["entries"][0]["content_fingerprint"]
        with pytest.raises(IntegrityError):
            ReplayGate().evaluate(baseline=a, candidate=tampered2)


class TestHardGates:
    def test_failed_request_hard_gate(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        b = BASE(
            tmp_path,
            "b",
            "tag",
            seed=1,
            results={"req-1": mk_result(status="error", error_kind="http_error", error="x")},
        )
        res = ReplayGate().evaluate(baseline=a, candidate=b)
        assert res.decision == GateDecision.FAIL
        assert res.exit_code == 1
        assert "request_success_below_min" in res.reason_codes

    def test_tool_failure_hard_gate(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        b = BASE(
            tmp_path,
            "b",
            "tag",
            seed=1,
            results={"req-0": mk_result(tools=[{"name": "t", "status": "crashed"}])},
        )
        res = ReplayGate().evaluate(baseline=a, candidate=b)
        assert res.decision == GateDecision.FAIL
        assert "tool_failure" in res.reason_codes

    def test_quality_below_min_hard_gate(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        b = BASE(
            tmp_path,
            "b",
            "tag",
            seed=1,
            results={"req-0": mk_result(quality={"pass_rate": 0.89})},
        )
        res = ReplayGate().evaluate(baseline=a, candidate=b)
        assert res.decision == GateDecision.FAIL
        assert "quality_below_min" in res.reason_codes


class TestThresholds:
    def test_regression_worse_than_threshold_fails(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        b = BASE(
            tmp_path,
            "b",
            "tag",
            seed=1,
            results={
                f"req-{i}": mk_result(ttft=0.5, decode=40.0, e2e=38.0) for i in range(4)
            },
        )
        res = ReplayGate().evaluate(baseline=a, candidate=b)
        assert res.decision == GateDecision.PASS

        # candidate: 12% slower ttft (0.56 vs 0.5) -> beyond 10% regression
        worse = BASE(
            tmp_path,
            "c",
            "tag",
            seed=1,
            results={f"req-{i}": mk_result(ttft=0.56) for i in range(4)},
        )
        res2 = ReplayGate().evaluate(baseline=a, candidate=worse)
        assert res2.decision == GateDecision.FAIL
        assert "ttft_s_regression" in res2.reason_codes

    def test_improvement_beyond_threshold_passes(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        better = BASE(
            tmp_path,
            "c",
            "tag",
            seed=1,
            results={f"req-{i}": mk_result(ttft=0.4) for i in range(4)},
        )
        res = ReplayGate().evaluate(baseline=a, candidate=better)
        assert res.decision == GateDecision.PASS
        assert "ttft_s_improvement" not in res.reason_codes

    def test_within_threshold_passes(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        slightly = BASE(
            tmp_path,
            "c",
            "tag",
            seed=1,
            results={f"req-{i}": mk_result(ttft=0.55) for i in range(4)},
        )  # 10% exactly is NOT a regression (strict >)
        res = ReplayGate().evaluate(baseline=a, candidate=slightly)
        assert res.decision == GateDecision.PASS

    def test_custom_thresholds_respected(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        slightly = BASE(
            tmp_path,
            "c",
            "tag",
            seed=1,
            results={f"req-{i}": mk_result(ttft=0.55) for i in range(4)},
        )
        gate = ReplayGate(thresholds={"ttft_s": GateThreshold(regression=0.10, improvement=0.20)})
        res = gate.evaluate(baseline=a, candidate=slightly)
        # Relative delta is exactly 0.10; strict > comparison means PASS.
        assert res.decision == GateDecision.PASS

    def test_threshold_validation(self) -> None:
        with pytest.raises(GateError):
            GateThreshold(regression=-0.1, improvement=0.1)
        with pytest.raises(GateError):
            GateThreshold(regression=0.1, improvement=float("nan"))
        with pytest.raises(GateError):
            ReplayGate(thresholds={"nope": GateThreshold()})

    def test_quality_and_usage_aggregates(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        b = BASE(
            tmp_path,
            "b",
            "tag",
            seed=1,
            results={
                f"req-{i}": mk_result(quality={"pass_rate": 1.0 - 0.04 * i},
                                     pt=10 + i, ct=100 + i)
                for i in range(4)
            },
        )
        res = ReplayGate().evaluate(baseline=a, candidate=b)
        assert res.decision == GateDecision.FAIL
        assert "quality_below_min" in res.reason_codes
        assert res.metrics.get("quality_pass_rate_min") is not None
        assert res.metrics.get("prompt_tokens_total") == 10 * 4 + 6  # 10+11+12+13

    def test_missing_usage_is_inconclusive(self, tmp_path) -> None:
        a = BASE(
            tmp_path,
            "a",
            "tag",
            seed=1,
            results={f"req-{i}": mk_result() for i in range(4)},
        )
        b = BASE(
            tmp_path,
            "b",
            "tag",
            seed=1,
            results={
                f"req-{i}": mk_result(usage=None) for i in range(4)
            },
        )
        res = ReplayGate().evaluate(baseline=a, candidate=b)
        assert res.decision == GateDecision.INCONCLUSIVE
        assert "usage_missing" in res.reason_codes


class TestDigestAndSummary:
    def test_canonical_digest_deterministic(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        b = BASE(tmp_path, "b", "tag", seed=1)
        r1 = ReplayGate().evaluate(baseline=a, candidate=b)
        r2 = ReplayGate().evaluate(baseline=copy.deepcopy(a), candidate=copy.deepcopy(b))
        assert r1.digest == r2.digest
        assert canonical_digest(r1.summary_payload) == r1.digest

    def test_summary_payload_shape(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        worse = BASE(
            tmp_path,
            "c",
            "tag",
            seed=1,
            results={f"req-{i}": mk_result(ttft=0.6) for i in range(4)},
        )
        res = ReplayGate().evaluate(baseline=a, candidate=worse)
        p = res.summary_payload
        assert p["verdict"] == "FAIL"
        assert p["digest"] == res.digest
        assert p["workload_hash"] == a["workload_hash"]
        assert p["protocol_hash"] == a["protocol"]["protocol_hash"]
        assert p["exit_code"] == 1
        assert len(p["reasons"]) > 0
        text = "\n".join(p["lines"])
        assert "FAIL" in text
        assert "ttft_s" in text
        assert len(p["lines"]) <= 15

    def test_summary_and_result_repr_never_leak_raw_content(self, tmp_path) -> None:
        cases = [make_case(f"req-{i}", content=f"prompt {i} secret={SECRET}") for i in range(4)]
        a = BASE(tmp_path, "a", "tag", cases=cases, seed=1)
        b = BASE(tmp_path, "b", "tag", cases=cases, seed=1)
        res = ReplayGate().evaluate(baseline=a, candidate=b)
        dump = json.dumps(res.summary_payload, sort_keys=True)
        assert SECRET not in dump
        assert SECRET not in repr(res)
        # the workload hash itself must not be a raw-content leak either
        assert SECRET not in res.workload_hash

    def test_exit_semantics(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        assert ReplayGate().evaluate(baseline=a, candidate=BASE(tmp_path, "b", "tag", seed=1)).exit_code == 0
        worse = BASE(
            tmp_path, "c", "tag", seed=1,
            results={f"req-{i}": mk_result(ttft=0.7) for i in range(4)},
        )
        assert ReplayGate().evaluate(baseline=a, candidate=worse).exit_code == 1
        incon = BASE(tmp_path, "d", "tag", seed=2)
        assert ReplayGate().evaluate(baseline=a, candidate=incon).exit_code == 0

    def test_default_thresholds_exposed(self) -> None:
        assert DEFAULT_THRESHOLDS["ttft_s"].regression == 0.10
        assert DEFAULT_THRESHOLDS["ttft_s"].improvement == 0.20
        assert DEFAULT_TOLERANCE == 1e-9


class TestDeterminism:
    def test_evaluator_deterministic(self, tmp_path) -> None:
        a = BASE(tmp_path, "a", "tag", seed=1)
        b = BASE(tmp_path, "b", "tag", seed=1)
        r1 = ReplayGate().evaluate(baseline=a, candidate=b)
        r2 = ReplayGate().evaluate(baseline=a, candidate=b)
        assert r1 == r2
