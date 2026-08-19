"""Workload loading, bounds, strict schema and deterministic sampling (RED first)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from serving_verdict.errors import WorkloadError
from serving_verdict.workload import (
    WORKLOAD_SCHEMA_VERSION,
    content_fingerprint,
    load_workload,
    sample_cases,
)
from tests.helpers import make_case, make_jsonl_workload

SECRET = "sk-live-SECRET-abcdef-9f8e7d6c5b4a"


def _write(tmp_path: Path, text: str, name: str = "workload.jsonl") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _case_line(**overrides: object) -> str:
    doc = make_case("req-0", content="hello world")
    doc.update(overrides)
    return json.dumps(doc)


class TestLoadBounds:
    def test_loads_minimal_jsonl(self, tmp_path: Path) -> None:
        wl = load_workload(_write(tmp_path, make_jsonl_workload()))
        assert wl.schema_version == WORKLOAD_SCHEMA_VERSION
        assert len(wl.cases) == 3
        assert wl.workload_hash.startswith("sha256:")
        assert wl.cases[0].request_id == "req-0"
        assert wl.cases[0].messages[0].content == "prompt 0"

    def test_trailing_newline_and_blank_lines_ok(self, tmp_path: Path) -> None:
        text = make_jsonl_workload() + "\n\n"
        wl = load_workload(_write(tmp_path, text))
        assert len(wl.cases) == 3

    def test_rejects_empty_file(self, tmp_path: Path) -> None:
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, ""))

    def test_rejects_blank_only_file(self, tmp_path: Path) -> None:
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, "\n  \n"))

    def test_rejects_too_many_cases_and_hides_content(self, tmp_path: Path) -> None:
        path = _write(tmp_path, make_jsonl_workload())
        with pytest.raises(WorkloadError) as exc:
            load_workload(path, max_cases=2)
        msg = str(exc.value)
        assert "prompt 0" not in msg
        assert "prompt 2" not in msg

    def test_rejects_file_over_size_bound_without_path(self, tmp_path: Path) -> None:
        path = _write(tmp_path, make_jsonl_workload())
        with pytest.raises(WorkloadError) as exc:
            load_workload(path, max_bytes=8)
        assert str(tmp_path) not in str(exc.value)

    def test_rejects_oversized_string_and_hides_content(self, tmp_path: Path) -> None:
        text = _case_line(content=f"lead {SECRET} tail")
        path = _write(tmp_path, text)
        with pytest.raises(WorkloadError) as exc:
            load_workload(path, max_string_chars=10)
        assert SECRET not in str(exc.value)

    def test_rejects_invalid_utf8(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_bytes(b'\xff\xfe{"request_id": "x"}')
        with pytest.raises(WorkloadError):
            load_workload(path)

    def test_rejects_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(WorkloadError):
            load_workload(tmp_path / "nope.jsonl")
        with pytest.raises(WorkloadError):
            load_workload(tmp_path)  # a directory, not a file


class TestStrictSchema:
    @pytest.mark.parametrize(
        "line",
        [
            _case_line(extra_key=1),  # unknown top-level key
            json.dumps({"messages": []}),  # missing request_id
            json.dumps({"request_id": "req-0"}),  # missing messages
            json.dumps([1, 2]),  # not an object
            json.dumps({"request_id": "req-0", "messages": "nope"}),  # messages not a list
            json.dumps({"request_id": "req-0", "messages": []}),  # zero messages
        ],
    )
    def test_rejects_malformed_case(self, tmp_path: Path, line: str) -> None:
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, line))

    def test_rejects_duplicate_request_id_without_echo(self, tmp_path: Path) -> None:
        text = _case_line(request_id=SECRET) + "\n" + _case_line(request_id=SECRET)
        with pytest.raises(WorkloadError) as exc:
            load_workload(_write(tmp_path, text))
        assert SECRET not in str(exc.value)

    def test_rejects_empty_and_overlong_request_id(self, tmp_path: Path) -> None:
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, _case_line(request_id="")))
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, _case_line(request_id="r" * 200)))

    @pytest.mark.parametrize(
        "role",
        ["system", "user", "assistant", "tool"],
        ids=["system", "user", "assistant", "tool"],
    )
    def test_accepts_allowed_roles(self, tmp_path: Path, role: str) -> None:
        msgs = [{"role": role, "content": "x"}]
        if role == "tool":
            msgs[0]["tool_call_id"] = "call_1"
        line = json.dumps({"request_id": "req-0", "messages": msgs})
        wl = load_workload(_write(tmp_path, line))
        assert wl.cases[0].messages[0].role == role

    def test_rejects_unknown_role(self, tmp_path: Path) -> None:
        line = json.dumps(
            {"request_id": "req-0", "messages": [{"role": "function", "content": "x"}]}
        )
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, line))

    def test_rejects_non_string_content(self, tmp_path: Path) -> None:
        line = json.dumps(
            {"request_id": "req-0", "messages": [{"role": "user", "content": 42}]}
        )
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, line))

    def test_rejects_list_content(self, tmp_path: Path) -> None:
        line = json.dumps(
            {
                "request_id": "req-0",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "x"}]}],
            }
        )
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, line))

    def test_rejects_unknown_message_key(self, tmp_path: Path) -> None:
        line = json.dumps(
            {
                "request_id": "req-0",
                "messages": [{"role": "user", "content": "x", "cache": True}],
            }
        )
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, line))

    def test_tool_message_requires_tool_call_id(self, tmp_path: Path) -> None:
        line = json.dumps(
            {"request_id": "req-0", "messages": [{"role": "tool", "content": "x"}]}
        )
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, line))

    def test_tool_message_must_not_carry_tool_calls(self, tmp_path: Path) -> None:
        line = json.dumps(
            {
                "request_id": "req-0",
                "messages": [
                    {
                        "role": "tool",
                        "content": "x",
                        "tool_call_id": "call_1",
                        "tool_calls": [{"id": "call_1", "name": "t", "arguments": "{}"}],
                    }
                ],
            }
        )
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, line))

    def test_assistant_tool_calls_strict_schema(self, tmp_path: Path) -> None:
        ok = {
            "request_id": "req-0",
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call_1", "name": "get_weather", "arguments": "{}"}],
                }
            ],
        }
        wl = load_workload(_write(tmp_path, json.dumps(ok)))
        assert len(wl.cases[0].messages[0].tool_calls) == 1
        assert wl.cases[0].messages[0].tool_calls[0].name == "get_weather"

        bad = json.loads(json.dumps(ok))
        del bad["messages"][0]["tool_calls"][0]["arguments"]  # missing arguments
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, json.dumps(bad)))

        bad2 = json.loads(json.dumps(ok))
        bad2["messages"][0]["tool_calls"][0]["extra"] = 1  # unknown tool_call key
        with pytest.raises(WorkloadError):
            load_workload(_write(tmp_path, json.dumps(bad2)))

    def test_rejects_non_json_line_without_content_echo(self, tmp_path: Path) -> None:
        with pytest.raises(WorkloadError) as exc:
            load_workload(_write(tmp_path, "{broken " + SECRET + " x"))
        assert SECRET not in str(exc.value)


class TestDeterministicSampling:
    def test_same_seed_same_sample(self, tmp_path: Path) -> None:
        big = [make_case(f"req-{i}", content=f"prompt {i}") for i in range(10)]
        wl10 = load_workload(_write(tmp_path, make_jsonl_workload(big), "big.jsonl"))
        a = sample_cases(wl10.cases, seed=42, n=4)
        b = sample_cases(wl10.cases, seed=42, n=4)
        assert [c.request_id for c in a] == [c.request_id for c in b]
        assert len(a) == 4
        assert {c.request_id for c in a} <= {c.request_id for c in wl10.cases}

    def test_different_seed_gives_different_order(self, tmp_path: Path) -> None:
        big = [make_case(f"req-{i}", content=f"prompt {i}") for i in range(10)]
        wl = load_workload(_write(tmp_path, make_jsonl_workload(big)))
        a = [c.request_id for c in sample_cases(wl.cases, seed=1, n=6)]
        b = [c.request_id for c in sample_cases(wl.cases, seed=2, n=6)]
        assert a != b

    def test_sample_larger_than_available_fails(self, tmp_path: Path) -> None:
        wl = load_workload(_write(tmp_path, make_jsonl_workload()))
        with pytest.raises(WorkloadError):
            sample_cases(wl.cases, seed=7, n=4)

    def test_sample_zero_or_negative_fails(self, tmp_path: Path) -> None:
        wl = load_workload(_write(tmp_path, make_jsonl_workload()))
        with pytest.raises(WorkloadError):
            sample_cases(wl.cases, seed=7, n=0)
        with pytest.raises(WorkloadError):
            sample_cases(wl.cases, seed=7, n=-1)

    def test_sample_does_not_mutate_workload(self, tmp_path: Path) -> None:
        wl = load_workload(_write(tmp_path, make_jsonl_workload()))
        before = wl.workload_hash
        sample_cases(wl.cases, seed=7, n=2)
        assert len(wl.cases) == 3
        assert wl.workload_hash == before


class TestWorkloadHash:
    def test_hash_stable_and_keyed(self, tmp_path: Path) -> None:
        text = make_jsonl_workload()
        h1 = load_workload(_write(tmp_path, text, "a.jsonl")).workload_hash
        h2 = load_workload(_write(tmp_path, text, "b.jsonl")).workload_hash
        assert h1 == h2
        assert h1.startswith("sha256:")
        assert len(h1) == len("sha256:") + 64

    def test_hash_changes_with_content(self, tmp_path: Path) -> None:
        big = [make_case(f"req-{i}", content=f"prompt {i}") for i in range(10)]
        wl1 = load_workload(_write(tmp_path, make_jsonl_workload(), "a.jsonl"))
        wl2 = load_workload(_write(tmp_path, make_jsonl_workload(big), "b.jsonl"))
        assert wl1.workload_hash != wl2.workload_hash

    def test_hash_is_not_plain_sha256_of_file(self, tmp_path: Path) -> None:
        import hashlib

        path = _write(tmp_path, make_jsonl_workload())
        wl = load_workload(path)
        plain = hashlib.sha256(path.read_bytes()).hexdigest()
        assert wl.workload_hash != "sha256:" + plain


class TestFingerprint:
    def test_fingerprint_is_keyed_and_stable(self) -> None:
        assert content_fingerprint("abc") == content_fingerprint("abc")
        assert content_fingerprint("abc").startswith("sha256kf:")

    def test_fingerprint_differs_from_plain_sha256(self) -> None:
        import hashlib

        plain = hashlib.sha256(b"abc").hexdigest()
        assert content_fingerprint("abc") != "sha256kf:" + plain

    def test_fingerprint_is_truncated_and_non_reversible(self) -> None:
        fp = content_fingerprint("a much longer text than needed for inversion")
        assert len(fp) == len("sha256kf:") + 32

    def test_fingerprint_distinct_for_distinct_inputs(self) -> None:
        assert content_fingerprint("abc") != content_fingerprint("abd")
