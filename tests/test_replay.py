"""Privacy-safe replay execution with injected executor (RED first).

Privacy contract under test:
- raw message content may exist only in memory during the executor call;
- the artifact, its JSON, its repr and every error message carry NO raw
  content, credentials, remote output or filesystem paths;
- redaction (user callback) runs strictly before the executor;
- the engine performs no I/O persistence.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from serving_verdict.errors import IntegrityError, ReplayError
from serving_verdict.replay import (
    PROTOCOL_VERSION,
    RawExecutionResult,
    ReplayArtifact,
    run_replay,
    verify_artifact,
)
from serving_verdict.workload import load_workload
from tests.helpers import make_case, make_jsonl_workload

SECRET = "sk-live-SECRET-abcdef-9f8e7d6c5b4a"
RAW_OUTPUT = "REMOTE-RAW-OUTPUT-777-xyz"


def ok_result(**overrides: object) -> RawExecutionResult:
    doc: dict[str, object] = {
        "status": "success",
        "ttft_s": 0.5,
        "decode_tokens_per_s": 40.0,
        "e2e_tokens_per_s": 38.0,
        "usage": {"prompt_tokens": 10, "completion_tokens": 100},
        "quality": {"pass_rate": 1.0},
    }
    doc.update(overrides)
    return RawExecutionResult(**doc)  # type: ignore[arg-type]


class FakeExecutor:
    """Records what it is handed; returns scripted results."""

    def __init__(
        self,
        results: dict[str, RawExecutionResult] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.results = results or {}
        self.error = error
        self.calls: list[tuple[str, list[dict[str, object]]]] = []

    def execute(
        self, case: object, messages: list[dict[str, object]]
    ) -> RawExecutionResult:
        self.calls.append((str(case.request_id), copy.deepcopy(messages)))
        if self.error is not None:
            raise self.error
        rid = str(case.request_id)
        if rid in self.results:
            return self.results[rid]
        return ok_result()


def _load(tmp_path: Path, content: str | None = None) -> object:
    cases = [make_case(f"req-{i}", content=content or f"prompt {i}") for i in range(3)]
    path = tmp_path / "workload.jsonl"
    path.write_text(make_jsonl_workload(cases), encoding="utf-8")
    return load_workload(path)


def _run(tmp_path: Path, executor: FakeExecutor, **kwargs: object) -> ReplayArtifact:
    wl = _load(tmp_path)
    return run_replay(
        wl,  # type: ignore[arg-type]
        executor,
        executor_tag="fixture-executor-v1",
        redaction_policy=str(kwargs.pop("redaction_policy", "none")),
        **kwargs,
    )


class TestArtifactPrivacy:
    def test_no_secret_in_artifact_json_or_repr(self, tmp_path: Path) -> None:
        path = tmp_path / "wl.jsonl"
        path.write_text(make_jsonl_workload([make_case("req-0", content=f"x {SECRET} y")]), encoding="utf-8")
        wl = load_workload(path)
        art = run_replay(wl, FakeExecutor(), executor_tag="tag", redaction_policy="none")
        dumped = json.dumps(art.to_payload(), sort_keys=True)
        assert SECRET not in dumped
        assert SECRET not in repr(art)
        assert SECRET not in repr(art.entries[0])

    def test_fingerprint_present_and_non_raw(self, tmp_path: Path) -> None:
        path = tmp_path / "wl.jsonl"
        path.write_text(make_jsonl_workload([make_case("req-0", content=f"x {SECRET} y")]), encoding="utf-8")
        wl = load_workload(path)
        art = run_replay(wl, FakeExecutor(), executor_tag="tag", redaction_policy="none")
        entry = art.entries[0]
        assert entry.content_fingerprint.startswith("sha256kf:")
        assert entry.redacted_fingerprint == entry.content_fingerprint
        assert entry.redaction_changed is False
        assert SECRET not in entry.content_fingerprint

    def test_no_file_persistence(self, tmp_path: Path) -> None:
        _run(tmp_path, FakeExecutor())
        files = {p.name for p in tmp_path.iterdir()}
        assert files == {"workload.jsonl"}  # no new artifact files created


class TestRedaction:
    def test_redaction_applied_before_executor(self, tmp_path: Path) -> None:
        path = tmp_path / "wl.jsonl"
        path.write_text(
            make_jsonl_workload(
                [make_case("req-0", content=f"password={SECRET} rest"), make_case("req-1")]
            ),
            encoding="utf-8",
        )
        wl = load_workload(path)
        seen: list[list[dict[str, object]]] = []

        def redact(case: object) -> list[dict[str, object]]:
            out: list[dict[str, object]] = []
            for m in case.messages:  # type: ignore[attr-defined]
                mm: dict[str, object] = {
                    "role": m.role,
                    "content": str(m.content).replace(SECRET, "[REDACTED]"),
                }
                if m.tool_call_id is not None:
                    mm["tool_call_id"] = m.tool_call_id
                if m.tool_calls:
                    mm["tool_calls"] = [
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in m.tool_calls
                    ]
                out.append(mm)
            seen.append(copy.deepcopy(out))
            return out

        ex = FakeExecutor()
        art = run_replay(
            wl,
            ex,
            executor_tag="tag",
            redaction_policy="redact-secrets-v1",
            redact=redact,
        )
        # executor only ever saw redacted content
        redaction_seen = False
        for _rid, msgs in ex.calls:
            for m in msgs:
                assert SECRET not in str(m)
                redaction_seen = redaction_seen or "[REDACTED]" in str(m["content"])
        assert redaction_seen
        # redaction was actually invoked before execution
        assert len(seen) == 2
        entry = art.entries[0]
        assert entry.redaction_changed is True
        assert entry.content_fingerprint != entry.redacted_fingerprint
        assert art.to_payload()["protocol"]["redaction_policy"] == "redact-secrets-v1"

    def test_redaction_structure_violation_fails_closed(self, tmp_path: Path) -> None:
        wl = _load(tmp_path, content=f"x {SECRET} y")

        def bad_redact(case: object) -> list[dict[str, object]]:
            return []  # drops all messages

        ex = FakeExecutor()
        with pytest.raises(ReplayError) as exc:
            run_replay(
                wl,
                ex,
                executor_tag="tag",
                redaction_policy="bad",
                redact=bad_redact,
            )
        assert ex.calls == []  # executor never invoked
        assert SECRET not in str(exc.value)

    def test_redaction_callback_exception_is_sanitized(self, tmp_path: Path) -> None:
        wl = _load(tmp_path, content=f"x {SECRET} y")

        def boom(case: object) -> list[dict[str, object]]:
            raise ValueError(f"raw context {SECRET} and {RAW_OUTPUT}")

        with pytest.raises(ReplayError) as exc:
            run_replay(wl, FakeExecutor(), executor_tag="tag", redaction_policy="x", redact=boom)
        msg = str(exc.value)
        assert SECRET not in msg
        assert RAW_OUTPUT not in msg


class TestNormalization:
    def test_successful_facts_normalized(self, tmp_path: Path) -> None:
        art = _run(tmp_path, FakeExecutor())
        e = art.entries[0]
        assert e.status == "succeeded"
        assert e.ttft_s == 0.5
        assert e.decode_tokens_per_s == 40.0
        assert e.e2e_tokens_per_s == 38.0
        assert e.completion_tokens == 100
        assert e.usage_present is True
        assert e.tool_status is None
        assert e.quality == {"pass_rate": 1.0}
        assert art.protocol_hash.startswith("sha256:")
        assert art.workload_hash.startswith("sha256:")
        assert art.to_payload()["schema_version"] == ReplayArtifact.SCHEMA_VERSION

    def test_digest_and_tamper(self, tmp_path: Path) -> None:
        art = _run(tmp_path, FakeExecutor())
        payload = art.to_payload()
        verify_artifact(payload, art.bundle_digest)
        tampered = copy.deepcopy(payload)
        tampered["entries"][0]["ttft_s"] = 0.1
        with pytest.raises(IntegrityError):
            verify_artifact(tampered, art.bundle_digest)
        tampered2 = copy.deepcopy(payload)
        tampered2["entries"][0]["request_id"] = "evil"
        with pytest.raises(IntegrityError):
            verify_artifact(tampered2, art.bundle_digest)

    def test_executor_exception_sanitized(self, tmp_path: Path) -> None:
        ex = FakeExecutor(error=RuntimeError(f"upstream 500 {RAW_OUTPUT} key={SECRET}"))
        art = _run(tmp_path, ex)
        for e in art.entries:
            assert e.status == "failed"
            assert e.error_kind == "executor_error"
            assert SECRET not in str(e)
            assert RAW_OUTPUT not in str(e)
            assert SECRET not in json.dumps(art.to_payload())
            assert RAW_OUTPUT not in json.dumps(art.to_payload())

    def test_remote_error_text_never_copied(self, tmp_path: Path) -> None:
        results = {
            "req-1": ok_result(
                status="error",
                error_kind="http_error",
                error=f"body: {RAW_OUTPUT} secret {SECRET}",
            )
        }
        art = _run(tmp_path, FakeExecutor(results))
        e = art.entries[1]
        assert e.status == "failed"
        assert e.error_kind == "http_error"
        dumped = json.dumps(art.to_payload(), sort_keys=True)
        assert RAW_OUTPUT not in dumped
        assert SECRET not in dumped

    def test_unknown_error_kind_collapses_to_unknown(self, tmp_path: Path) -> None:
        results = {
            "req-0": ok_result(status="error", error_kind="totally_new_kind", error="x")
        }
        art = _run(tmp_path, FakeExecutor(results))
        assert art.entries[0].error_kind == "unknown"

    def test_missing_usage_recorded(self, tmp_path: Path) -> None:
        results = {"req-0": ok_result(usage=None)}
        art = _run(tmp_path, FakeExecutor(results))
        assert art.entries[0].usage_present is False
        assert art.entries[0].completion_tokens is None
        assert art.entries[1].usage_present is True

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
    def test_bad_metric_values_fail_closed(self, tmp_path: Path, bad: float) -> None:
        ex = FakeExecutor(error=None)
        ex.results = {"req-0": ok_result(ttft_s=bad)}
        with pytest.raises(ReplayError):
            _run(tmp_path, ex)

    @pytest.mark.parametrize(
        "bad_usage",
        [
            {"prompt_tokens": 1.5, "completion_tokens": 2},  # non-int
            {"prompt_tokens": 1},  # missing key
            {"prompt_tokens": 1, "completion_tokens": 2, "extra": 3},  # extra key
            {"prompt_tokens": -1, "completion_tokens": 2},  # negative
        ],
    )
    def test_malformed_usage_fails_closed(self, tmp_path: Path, bad_usage: dict[str, object]) -> None:
        ex = FakeExecutor()
        ex.results = {"req-0": ok_result(usage=bad_usage)}
        with pytest.raises(ReplayError):
            _run(tmp_path, ex)

    def test_bad_status_fails_closed(self, tmp_path: Path) -> None:
        ex = FakeExecutor()
        ex.results = {"req-0": ok_result(status="weird")}
        with pytest.raises(ReplayError):
            _run(tmp_path, ex)

    def test_tool_facts_normalized(self, tmp_path: Path) -> None:
        results = {
            "req-0": ok_result(
                tools=[
                    {"name": "get_weather", "status": "ok"},
                    {"name": "db.query", "status": "ok"},
                ]
            ),
            "req-1": ok_result(tools=[{"name": "db.query", "status": "crashed"}]),
        }
        art = _run(tmp_path, FakeExecutor(results))
        assert art.entries[0].tool_status == "ok"
        assert art.entries[0].tool_success is True
        # unknown tool status fails closed as error
        assert art.entries[1].tool_status == "error"
        assert art.entries[1].tool_success is False

    @pytest.mark.parametrize(
        "bad_quality",
        [
            {"Pass_Rate": 1.0},  # bad key charset
            {"pass_rate": 1.5},  # out of [0,1]
            {"pass_rate": float("nan")},
        ],
    )
    def test_bad_quality_fails_closed(self, tmp_path: Path, bad_quality: dict[str, float]) -> None:
        ex = FakeExecutor()
        ex.results = {"req-0": ok_result(quality=bad_quality)}
        with pytest.raises(ReplayError):
            _run(tmp_path, ex)

    def test_bad_protocol_tags_rejected(self, tmp_path: Path) -> None:
        wl = _load(tmp_path)
        with pytest.raises(ReplayError):
            run_replay(wl, FakeExecutor(), executor_tag="", redaction_policy="none")  # type: ignore[arg-type]
        with pytest.raises(ReplayError):
            run_replay(
                wl, FakeExecutor(), executor_tag="t" * 200, redaction_policy="none"
            )  # type: ignore[arg-type]

    def test_protocol_constant_exposed(self) -> None:
        assert PROTOCOL_VERSION.startswith("serving-verdict.replay-protocol.")


class TestDeterminism:
    def test_same_seed_same_artifact_digest(self, tmp_path: Path) -> None:
        big = [make_case(f"req-{i}", content=f"prompt {i}") for i in range(10)]
        path = tmp_path / "wl.jsonl"
        path.write_text(make_jsonl_workload(big), encoding="utf-8")
        wl = load_workload(path)
        a = run_replay(
            wl, FakeExecutor(), executor_tag="tag", redaction_policy="none",
            seed=42, sample_size=4,
        )
        b = run_replay(
            load_workload(path),
            FakeExecutor(),
            executor_tag="tag",
            redaction_policy="none",
            seed=42,
            sample_size=4,
        )
        assert a.bundle_digest == b.bundle_digest
        assert [e.request_id for e in a.entries] == [e.request_id for e in b.entries]

    def test_different_seed_changes_sample(self, tmp_path: Path) -> None:
        big = [make_case(f"req-{i}", content=f"prompt {i}") for i in range(10)]
        path = tmp_path / "wl.jsonl"
        path.write_text(make_jsonl_workload(big), encoding="utf-8")
        wl = load_workload(path)
        a = run_replay(
            wl, FakeExecutor(), executor_tag="tag", redaction_policy="none",
            seed=1, sample_size=5,
        )
        b = run_replay(
            wl, FakeExecutor(), executor_tag="tag", redaction_policy="none",
            seed=2, sample_size=5,
        )
        assert [e.request_id for e in a.entries] != [e.request_id for e in b.entries]

    def test_sample_bounds_enforced(self, tmp_path: Path) -> None:
        wl = _load(tmp_path)
        with pytest.raises(ReplayError):
            run_replay(
                wl, FakeExecutor(), executor_tag="tag", redaction_policy="none",
                seed=1, sample_size=99,
            )  # type: ignore[arg-type]
