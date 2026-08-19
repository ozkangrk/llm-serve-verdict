"""Bounded, strict workload loading with deterministic sampling.

Privacy contract:
- Error messages are structural only (bounds, key names, line numbers).
  Raw message content, values and file paths are NEVER included.
- Nothing is persisted: the workload lives only in memory.
- Content is fingerprinted with a keyed, truncated, non-reversible digest.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from serving_verdict.canonical import canonicalize
from serving_verdict.errors import ReplayError, WorkloadError

WORKLOAD_SCHEMA_VERSION = "serving-verdict.workload.v1"

MAX_CASES: int = 10_000
MAX_FILE_BYTES: int = 32 * 1024 * 1024  # 32 MiB
MAX_CASE_BYTES: int = 1 * 1024 * 1024  # 1 MiB per case line
MAX_MESSAGES_PER_CASE: int = 64
MAX_STRING_CHARS: int = 128 * 1024  # 128 KiB per string field
MAX_REQUEST_ID: int = 128
MAX_TOOL_CALLS: int = 32

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

ALLOWED_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})
_ALLOWED_TOP_KEYS: frozenset[str] = frozenset({"request_id", "messages"})
_ALLOWED_MSG_KEYS: frozenset[str] = frozenset({"role", "content", "tool_call_id", "tool_calls"})
_ALLOWED_TOOLCALL_KEYS: frozenset[str] = frozenset({"id", "name", "arguments"})

_FINGERPRINT_KEY = b"serving-verdict.workload.fingerprint.v1"
_WORKLOAD_HASH_KEY = b"serving-verdict.workload.hash.v1"


def content_fingerprint(text: str) -> str:
    """Keyed, truncated, non-reversible fingerprint of content (16-byte hex)."""
    digest = hashlib.sha256(_FINGERPRINT_KEY + b"\x00" + text.encode("utf-8")).digest()
    return "sha256kf:" + digest[:16].hex()


def validate_tag(tag: str, name: str) -> str:
    """Validate a protocol tag (executor tag / redaction policy).

    Raises ReplayError (sanitized) on violation; never echoes the value.
    """
    if not tag or len(tag) > MAX_REQUEST_ID or not _TAG_RE.fullmatch(tag):
        raise ReplayError(
            f"{name} must be a 1-{MAX_REQUEST_ID} char tag of [A-Za-z0-9.:-]"
        )
    return tag


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class WorkloadMessage:
    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class WorkloadCase:
    request_id: str
    messages: tuple[WorkloadMessage, ...]

    def raw_messages(self) -> list[dict[str, Any]]:
        """In-memory copy of the (possibly in-memory-only) raw messages."""
        out: list[dict[str, Any]] = []
        for m in self.messages:
            d: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_call_id is not None:
                d["tool_call_id"] = m.tool_call_id
            if m.tool_calls:
                d["tool_calls"] = [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in m.tool_calls
                ]
            out.append(d)
        return out

    def content_key(self) -> str:
        """Structural key for the workload hash: no raw content, only fingerprints."""
        parts = [
            f"{m.role}:{content_fingerprint(m.content)}"
            + (f":{content_fingerprint(m.tool_call_id)}" if m.tool_call_id else "")
            for m in self.messages
        ]
        return self.request_id + "\x1f" + "\x1e".join(parts)


@dataclass(frozen=True)
class Workload:
    schema_version: str
    workload_hash: str
    size_bytes: int
    line_count: int
    cases: tuple[WorkloadCase, ...]


def _check_string(value: Any, field: str, max_len: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise WorkloadError(f"{field} must be a non-empty string of at most {max_len} chars")
    return value


def _parse_tool_calls(raw: Any) -> tuple[ToolCall, ...]:
    if not isinstance(raw, list) or not raw:
        raise WorkloadError("tool_calls must be a non-empty list")
    if len(raw) > MAX_TOOL_CALLS:
        raise WorkloadError(f"too many tool_calls (max {MAX_TOOL_CALLS})")
    out: list[ToolCall] = []
    for i, tc in enumerate(raw):
        if not isinstance(tc, dict) or set(tc.keys()) != _ALLOWED_TOOLCALL_KEYS:
            raise WorkloadError(f"tool_calls[{i}] has unexpected keys or type")
        tc_id = _check_string(tc["id"], f"tool_calls[{i}].id", MAX_REQUEST_ID)
        name = _check_string(tc["name"], f"tool_calls[{i}].name", MAX_REQUEST_ID)
        args = _check_string(tc["arguments"], f"tool_calls[{i}].arguments", MAX_STRING_CHARS)
        out.append(ToolCall(id=tc_id, name=name, arguments=args))
    return tuple(out)


def _parse_message(raw: Any, index: int) -> WorkloadMessage:
    if not isinstance(raw, dict) or set(raw.keys()) - _ALLOWED_MSG_KEYS:
        raise WorkloadError(
            f"messages[{index}] must be an object with keys subset of "
            f"{sorted(_ALLOWED_MSG_KEYS)}"
        )
    role = raw.get("role")
    if role not in ALLOWED_ROLES:
        raise WorkloadError(f"messages[{index}].role must be one of {sorted(ALLOWED_ROLES)}")
    content = raw.get("content")
    if not isinstance(content, str) or len(content) > MAX_STRING_CHARS:
        raise WorkloadError(
            f"messages[{index}].content must be a string of at most {MAX_STRING_CHARS} chars"
        )
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    if "tool_call_id" in raw:
        tool_call_id = _check_string(raw["tool_call_id"], f"messages[{index}].tool_call_id", 128)
        if role != "tool":
            raise WorkloadError("tool_call_id is only valid on tool messages")
        if "tool_calls" in raw:
            raise WorkloadError("tool messages must not carry tool_calls")
    if "tool_calls" in raw:
        if role != "assistant":
            raise WorkloadError("tool_calls is only valid on assistant messages")
        tool_calls = _parse_tool_calls(raw["tool_calls"])
    if role == "tool" and tool_call_id is None:
        raise WorkloadError("tool messages require tool_call_id")
    return WorkloadMessage(role=role, content=content, tool_call_id=tool_call_id, tool_calls=tool_calls)


def _parse_case(raw: Any, index: int, seen_ids: set[str]) -> WorkloadCase:
    if not isinstance(raw, dict) or set(raw.keys()) - _ALLOWED_TOP_KEYS:
        raise WorkloadError(
            f"case {index} must be an object with keys subset of {sorted(_ALLOWED_TOP_KEYS)}"
        )
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or not request_id or len(request_id) > MAX_REQUEST_ID:
        raise WorkloadError(
            f"case {index} request_id must be a non-empty string of at most {MAX_REQUEST_ID} chars"
        )
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise WorkloadError(
            f"case {index} request_id must match [A-Za-z0-9][A-Za-z0-9._-]*"
        )
    if request_id in seen_ids:
        raise WorkloadError(f"case {index} request_id is a duplicate")
    seen_ids.add(request_id)
    messages = raw.get("messages")
    if not isinstance(messages, list) or not messages:
        raise WorkloadError(f"case {index} messages must be a non-empty list")
    if len(messages) > MAX_MESSAGES_PER_CASE:
        raise WorkloadError(f"case {index} has too many messages (max {MAX_MESSAGES_PER_CASE})")
    parsed = tuple(_parse_message(m, i) for i, m in enumerate(messages))
    return WorkloadCase(request_id=request_id, messages=parsed)


def compute_workload_hash(
    cases: tuple[WorkloadCase, ...], line_count: int
) -> str:
    """Canonical hash over the structural workload key (never raw content)."""
    doc = {
        "schema_version": WORKLOAD_SCHEMA_VERSION,
        "line_count": line_count,
        "case_count": len(cases),
        "cases": [c.content_key() for c in cases],
    }
    return _hash_keyed(doc)


def _hash_keyed(doc: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        _WORKLOAD_HASH_KEY + b"\x00" + canonicalize(doc)
    ).hexdigest()


def load_workload(
    path: str | Path,
    *,
    max_cases: int = MAX_CASES,
    max_bytes: int = MAX_FILE_BYTES,
    max_string_chars: int = MAX_STRING_CHARS,
) -> Workload:
    """Load a JSONL workload file into typed in-memory cases.

    Strict and fail-closed; raises WorkloadError with a sanitized (structural)
    message on any violation. ``max_string_chars`` tightens the per-string bound.
    """
    p = Path(path)
    if p.is_dir():
        raise WorkloadError("workload path is a directory, not a file")
    if not p.is_file():
        raise WorkloadError("workload file not found")
    if p.stat().st_size > max_bytes:
        raise WorkloadError(f"workload exceeds size bound of {max_bytes} bytes")
    try:
        data = p.read_bytes()
    except OSError:
        raise WorkloadError("workload file cannot be read") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkloadError("workload is not valid UTF-8") from None

    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        raise WorkloadError("workload has no cases")
    if len(lines) > max_cases:
        raise WorkloadError(f"workload has more than {max_cases} cases")

    bound = min(MAX_STRING_CHARS, max_string_chars)
    seen: set[str] = set()
    cases: list[WorkloadCase] = []
    for i, line in enumerate(lines):
        if len(line.encode("utf-8")) > MAX_CASE_BYTES:
            raise WorkloadError(
                f"case {i} exceeds per-case size bound of {MAX_CASE_BYTES} bytes"
            )
        try:
            doc = json.loads(line)
        except ValueError:
            raise WorkloadError(f"case {i} is not valid JSON") from None
        if bound < MAX_STRING_CHARS:
            # Tight bound: scan parsed strings against it (fail-closed, no echo).
            for s in _iter_strings(doc):
                if len(s) > bound:
                    raise WorkloadError(
                        f"case {i} has a string longer than the bound of {bound} chars"
                    )
        cases.append(_parse_case(doc, i, seen))

    workload_hash = _hash_keyed(
        {
            "schema_version": WORKLOAD_SCHEMA_VERSION,
            "line_count": len(lines),
            "case_count": len(cases),
            "cases": [c.content_key() for c in cases],
        }
    )
    return Workload(
        schema_version=WORKLOAD_SCHEMA_VERSION,
        workload_hash=workload_hash,
        size_bytes=p.stat().st_size,
        line_count=len(lines),
        cases=tuple(cases),
    )


def _iter_strings(doc: Any) -> list[str]:
    out: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for k, val in v.items():
                if isinstance(k, str):
                    out.append(k)
                walk(val)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    walk(doc)
    return out


def sample_cases(cases: tuple[WorkloadCase, ...], *, seed: int, n: int) -> tuple[WorkloadCase, ...]:
    """Deterministic seeded sample of ``n`` distinct cases (stable ordering).

    Raises WorkloadError if n <= 0 or n > len(cases). Does not mutate anything.
    """
    if n <= 0:
        raise WorkloadError("sample size must be a positive integer")
    if n > len(cases):
        raise WorkloadError(f"sample size {n} exceeds available cases {len(cases)}")
    rng = random.Random(seed)
    idx = sorted(rng.sample(range(len(cases)), n))
    return tuple(cases[i] for i in idx)
