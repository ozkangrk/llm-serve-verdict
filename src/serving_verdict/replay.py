"""Privacy-safe replay execution with an injected executor.

Contract:
- The caller supplies an executor (``execute(case, messages) -> RawExecutionResult``).
  The engine passes in-memory message dicts and normalizes the raw result into
  bounded, typed facts.
- Optional user-supplied redaction callback runs strictly BEFORE the executor;
  its output is structure-validated (same shape, only content may change).
- The artifact stores only: request IDs, keyed/non-reversible content
  fingerprints, lengths, workload/protocol hashes, measurements and redaction
  provenance. Never raw messages, output, credentials or paths.
- The engine performs no I/O persistence of prompts.
- Every error message is sanitized: structural text only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol

from serving_verdict.canonical import canonicalize
from serving_verdict.errors import IntegrityError, ReplayError
from serving_verdict.workload import (
    MAX_STRING_CHARS,
    Workload,
    WorkloadCase,
    content_fingerprint,
    sample_cases,
    validate_tag,
)

PROTOCOL_VERSION = "serving-verdict.replay-protocol.v1"

_RAW_KEY: bytes = b"serving-verdict.replay.artifact.v1"

_ALLOWED_RAW_FIELDS: frozenset[str] = frozenset(
    {
        "status",
        "ttft_s",
        "decode_tokens_per_s",
        "e2e_tokens_per_s",
        "usage",
        "quality",
        "tools",
        "error_kind",
        "error",
    }
)

KNOWN_ERROR_KINDS: frozenset[str] = frozenset(
    {
        "timeout",
        "http_error",
        "rate_limited",
        "server_error",
        "client_error",
        "validation_error",
        "cancelled",
        "executor_error",
        "unknown",
    }
)

_ALLOWED_TOOL_STATUSES: frozenset[str] = frozenset({"ok", "error", "skipped"})

QUALITY_KEYS: frozenset[str] = frozenset({"pass_rate"})
_QUALITY_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_MAX_METRIC = 1e9  # sanity bound for latency/rate facts
_MAX_TOKENS = 1 << 30


@dataclass(frozen=True)
class RawExecutionResult:
    """Normalized raw result supplied by the injected executor.

    ``error`` is executor-side detail and is NEVER persisted; only ``error_kind``
    survives normalization.
    """

    status: str  # "success" | "error"
    ttft_s: float | None = None
    decode_tokens_per_s: float | None = None
    e2e_tokens_per_s: float | None = None
    usage: dict[str, int] | None = None
    quality: dict[str, float] | None = None
    tools: list[dict[str, str]] | None = None
    error_kind: str | None = None
    error: str | None = None  # never persisted


class ReplayExecutor(Protocol):
    def execute(self, case: WorkloadCase, messages: list[dict[str, Any]]) -> RawExecutionResult:
        ...


def _check_finite_nonneg(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReplayError(f"{name} must be a number")
    v = float(value)
    if not math.isfinite(v) or v < 0.0 or v > _MAX_METRIC:
        raise ReplayError(f"{name} is out of the valid measurement range")
    return v


def _normalize_usage(raw: dict[str, int] | None) -> tuple[bool, int | None, int | None]:
    if raw is None:
        return (False, None, None)
    if set(raw.keys()) != {"prompt_tokens", "completion_tokens"}:
        raise ReplayError("usage must contain exactly prompt_tokens and completion_tokens")
    for key in ("prompt_tokens", "completion_tokens"):
        v = raw[key]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > _MAX_TOKENS:
            raise ReplayError(f"usage.{key} must be a non-negative integer")
    return (True, int(raw["prompt_tokens"]), int(raw["completion_tokens"]))


def _normalize_quality(raw: dict[str, float] | None) -> dict[str, float]:
    if raw is None:
        return {}
    if not raw:
        raise ReplayError("quality must be a non-empty object")
    out: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _QUALITY_KEY_RE.fullmatch(key):
            raise ReplayError("quality keys must match [a-z][a-z0-9_]*")
        if key not in QUALITY_KEYS:
            raise ReplayError("quality contains an unknown key")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ReplayError(f"quality.{key} must be a number")
        v = float(value)
        if not math.isfinite(v) or v < 0.0 or v > 1.0:
            raise ReplayError(f"quality.{key} must be within [0, 1]")
        out[key] = v
    return out


def _normalize_tools(raw: list[dict[str, str]] | None) -> tuple[str | None, bool]:
    if raw is None:
        return (None, True)
    if not isinstance(raw, list) or not raw or len(raw) > 64:
        raise ReplayError("tools must be a non-empty list of at most 64 entries")
    for i, tool in enumerate(raw):
        if not isinstance(tool, dict) or set(tool.keys()) != {"name", "status"}:
            raise ReplayError(f"tools[{i}] must be an object with keys name and status")
        name = tool["name"]
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or not re.fullmatch(r"[A-Za-z0-9._:-]+", name)
        ):
            raise ReplayError(f"tools[{i}].name is malformed")
        if not isinstance(tool["status"], str) or not tool["status"]:
            raise ReplayError(f"tools[{i}].status must be a non-empty string")
    statuses = {t["status"] for t in raw}
    if "ok" in statuses and not statuses - {"ok"}:
        return ("ok", True)
    if not (statuses - {"ok", "skipped"}) and "skipped" in statuses:
        return ("skipped", True)
    # Any unknown or "error" status fails closed at the entry level.
    return ("error", False)


def _content_fingerprints(messages: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for m in messages:
        parts = [str(m.get("role", "")), str(m.get("content", ""))]
        if "tool_call_id" in m:
            parts.append(str(m.get("tool_call_id")))
        if "tool_calls" in m:
            parts.append(json.dumps(m.get("tool_calls"), sort_keys=True, ensure_ascii=True))
        out.append(content_fingerprint("\x1f".join(parts)))
    return out


def _validate_redacted(
    original: list[dict[str, Any]], redacted: list[dict[str, Any]]
) -> None:
    """Fail-closed structure check: redacted must mirror original exactly,
    with only content/tool_call arguments allowed to differ."""
    if not isinstance(redacted, list):
        raise ReplayError("redaction callback must return a list of messages")
    if len(redacted) != len(original):
        raise ReplayError("redaction changed the message count")
    for i, (o, r) in enumerate(zip(original, redacted, strict=True)):
        if not isinstance(r, dict) or set(r.keys()) != set(o.keys()):
            raise ReplayError(f"redaction changed the message keys at index {i}")
        for key in o:
            ov, rv = o[key], r[key]
            if key in ("role", "tool_call_id"):
                if not isinstance(rv, str) or rv != ov:
                    raise ReplayError(f"redaction changed field {key} at index {i}")
            elif key == "content":
                if not isinstance(rv, str) or not rv or len(rv) > MAX_STRING_CHARS:
                    raise ReplayError(f"redaction produced invalid content at index {i}")
            elif key == "tool_calls":
                if not isinstance(rv, list) or len(rv) != len(ov):
                    raise ReplayError(f"redaction changed tool_calls at index {i}")
                for j, (oc, rc) in enumerate(zip(ov, rv, strict=True)):
                    if not isinstance(rc, dict) or set(rc.keys()) != set(oc.keys()):
                        raise ReplayError(
                            f"redaction changed tool_calls[{j}] keys at index {i}"
                        )
                    for ck in oc:
                        if ck in ("id", "name"):
                            if rc.get(ck) != oc[ck]:
                                raise ReplayError(
                                    f"redaction changed tool_calls[{j}].{ck} at index {i}"
                                )
                        elif ck == "arguments":
                            if not isinstance(rc.get(ck), str) or not rc.get(ck):
                                raise ReplayError(
                                    f"redaction produced invalid tool_calls[{j}].arguments"
                                )
                        else:
                            if rc.get(ck) != oc[ck]:
                                raise ReplayError(
                                    f"redaction changed tool_calls[{j}].{ck} at index {i}"
                                )
            else:
                if r[key] != ov:
                    raise ReplayError(f"redaction changed field {key} at index {i}")


@dataclass(frozen=True)
class ReplayEntry:
    """One normalized, privacy-safe replay fact row.

    Carries only a request ID, keyed/non-reversible fingerprints, redaction
    provenance and bounded measurements. No raw content, ever.
    """

    request_id: str
    content_fingerprint: str
    redacted_fingerprint: str
    redaction_changed: bool
    status: str  # "succeeded" | "failed"
    error_kind: str | None
    ttft_s: float | None
    decode_tokens_per_s: float | None
    e2e_tokens_per_s: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    usage_present: bool
    tool_status: str | None
    tool_success: bool | None
    quality: dict[str, float]

    def to_payload(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def __repr__(self) -> str:
        return (
            f"ReplayEntry(request_id={self.request_id!r}, status={self.status!r}, "
            f"error_kind={self.error_kind!r}, usage_present={self.usage_present})"
        )


def _normalize_entry(
    raw: RawExecutionResult,
    request_id: str,
    content_fp: str,
    redacted_fp: str,
    redaction_changed: bool,
) -> ReplayEntry:
    if raw.status not in ("success", "error"):
        raise ReplayError("executor returned an invalid status")

    def _metric(value: float | None, name: str) -> float | None:
        v = _check_finite_nonneg(value, name)
        return v

    if raw.status == "error":
        kind = raw.error_kind if raw.error_kind in KNOWN_ERROR_KINDS else "unknown"
        return ReplayEntry(
            request_id=request_id,
            content_fingerprint=content_fp,
            redacted_fingerprint=redacted_fp,
            redaction_changed=redaction_changed,
            status="failed",
            error_kind=kind,
            ttft_s=None,
            decode_tokens_per_s=None,
            e2e_tokens_per_s=None,
            prompt_tokens=None,
            completion_tokens=None,
            usage_present=False,
            tool_status=None,
            tool_success=None,
            quality={},
        )

    usage_present, prompt_tokens, completion_tokens = _normalize_usage(raw.usage)
    tool_status, tool_success = _normalize_tools(raw.tools)
    quality = _normalize_quality(raw.quality)
    return ReplayEntry(
        request_id=request_id,
        content_fingerprint=content_fp,
        redacted_fingerprint=redacted_fp,
        redaction_changed=redaction_changed,
        status="succeeded",
        error_kind=None,
        ttft_s=_metric(raw.ttft_s, "ttft_s"),
        decode_tokens_per_s=_metric(raw.decode_tokens_per_s, "decode_tokens_per_s"),
        e2e_tokens_per_s=_metric(raw.e2e_tokens_per_s, "e2e_tokens_per_s"),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        usage_present=usage_present,
        tool_status=tool_status,
        tool_success=tool_success,
        quality=quality,
    )


_ALLOWED_ENTRY_KEYS: frozenset[str] = frozenset(
    {
        "request_id",
        "content_fingerprint",
        "redacted_fingerprint",
        "redaction_changed",
        "status",
        "error_kind",
        "ttft_s",
        "decode_tokens_per_s",
        "e2e_tokens_per_s",
        "prompt_tokens",
        "completion_tokens",
        "usage_present",
        "tool_status",
        "tool_success",
        "quality",
    }
)
_ALLOWED_PROTOCOL_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "executor_tag",
        "redaction_policy",
        "seed",
        "sample_size",
        "case_count",
        "protocol_hash",
    }
)


def _hash_artifact(doc: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_RAW_KEY + b"\x00" + canonicalize(doc)).hexdigest()


class ReplayArtifact:
    """Bounded, privacy-safe replay artifact.

    Exposes only request IDs, keyed fingerprints, lengths, hashes, normalized
    measurements and redaction provenance. ``to_payload`` is the canonical
    document; ``bundle_digest`` commits to it.
    """

    SCHEMA_VERSION = "serving-verdict.replay.v1"

    def __init__(
        self,
        *,
        protocol: dict[str, Any],
        workload_hash: str,
        size_bytes: int,
        entries: list[ReplayEntry],
    ) -> None:
        self._protocol = protocol
        self._workload_hash = workload_hash
        self._size_bytes = size_bytes
        self._entries = entries
        self.bundle_digest = self._compute_digest()

    def _compute_digest(self) -> str:
        return _hash_artifact(self.to_payload_without_digest())

    def to_payload_without_digest(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "protocol": dict(self._protocol),
            "workload_hash": self._workload_hash,
            "size_bytes": self._size_bytes,
            "entries": [e.to_payload() for e in self._entries],
        }

    def to_payload(self) -> dict[str, Any]:
        doc = self.to_payload_without_digest()
        doc["bundle_digest"] = self.bundle_digest
        return doc

    @property
    def protocol_hash(self) -> str:
        return str(self._protocol["protocol_hash"])

    @property
    def workload_hash(self) -> str:
        return self._workload_hash

    @property
    def entries(self) -> list[ReplayEntry]:
        return list(self._entries)

    def __repr__(self) -> str:  # privacy: no raw content, by construction
        return (
            f"ReplayArtifact(schema={self.SCHEMA_VERSION!r}, "
            f"digest={self.bundle_digest!r}, entries={len(self._entries)})"
        )


def verify_artifact(payload: dict[str, Any], expected_digest: str) -> None:
    """Structural + digest verification of an artifact payload (fail-closed).

    Raises IntegrityError on any structural deviation or digest mismatch.
    """
    if not isinstance(payload, dict):
        raise IntegrityError("artifact payload must be an object")
    if payload.get("schema_version") != ReplayArtifact.SCHEMA_VERSION:
        raise IntegrityError("artifact schema_version mismatch")
    protocol = payload.get("protocol")
    if not isinstance(protocol, dict) or set(protocol.keys()) != _ALLOWED_PROTOCOL_KEYS:
        raise IntegrityError("artifact protocol is malformed")
    if protocol["version"] != PROTOCOL_VERSION:
        raise IntegrityError("artifact protocol version mismatch")
    if not isinstance(payload.get("workload_hash"), str) or not isinstance(
        payload.get("size_bytes"), int
    ):
        raise IntegrityError("artifact workload metadata is malformed")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise IntegrityError("artifact entries are malformed")
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry.keys()) != _ALLOWED_ENTRY_KEYS:
            raise IntegrityError(f"artifact entry {i} is malformed")
        if entry.get("status") not in ("succeeded", "failed"):
            raise IntegrityError(f"artifact entry {i} has an invalid status")
        if not isinstance(entry.get("request_id"), str) or not entry["request_id"]:
            raise IntegrityError(f"artifact entry {i} has an invalid request_id")
        for fp_key in ("content_fingerprint", "redacted_fingerprint"):
            fp = entry.get(fp_key)
            if not isinstance(fp, str) or not fp.startswith("sha256kf:"):
                raise IntegrityError(f"artifact entry {i} has an invalid {fp_key}")
    doc = {k: v for k, v in payload.items() if k != "bundle_digest"}
    digest = _hash_artifact(doc)
    if digest != expected_digest:
        raise IntegrityError("artifact digest mismatch")
    if payload.get("bundle_digest") != expected_digest:
        raise IntegrityError("artifact bundle_digest field mismatch")


def run_replay(
    workload: Workload,
    executor: ReplayExecutor,
    *,
    executor_tag: str,
    redaction_policy: str = "none",
    redact: Any = None,
    seed: int | None = None,
    sample_size: int | None = None,
) -> ReplayArtifact:
    """Run the (sampled) workload through the injected executor.

    - ``redact(case) -> list[dict]`` is applied strictly before the executor and
      structure-validated (fail-closed).
    - ``sample_size`` requires ``seed`` and triggers deterministic sampling.
    - Normalization failures raise ReplayError with sanitized messages.
    - No persistence is performed.
    """
    validate_tag(executor_tag, "executor_tag")
    validate_tag(redaction_policy, "redaction_policy")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 0):
        raise ReplayError("seed must be a non-negative integer")
    if sample_size is not None:
        if seed is None:
            raise ReplayError("sample_size requires seed")
        if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size <= 0:
            raise ReplayError("sample_size must be a positive integer")
        if sample_size > len(workload.cases):
            raise ReplayError(
                f"sample_size {sample_size} exceeds available cases {len(workload.cases)}"
            )
        selected: tuple[WorkloadCase, ...] = sample_cases(
            workload.cases, seed=seed, n=sample_size
        )
    else:
        selected = workload.cases

    protocol_doc = {
        "version": PROTOCOL_VERSION,
        "executor_tag": executor_tag,
        "redaction_policy": redaction_policy,
        "seed": seed,
        "sample_size": sample_size,
        "case_count": len(selected),
    }
    protocol_hash = (
        "sha256:" + hashlib.sha256(_RAW_KEY + b"\x00" + canonicalize(protocol_doc)).hexdigest()
    )
    protocol = dict(protocol_doc, protocol_hash=protocol_hash)

    entries: list[ReplayEntry] = []
    for case in selected:
        original = case.raw_messages()
        content_fp = content_fingerprint("\x1f".join(_content_fingerprints(original)))
        effective = original
        redacted_fp = content_fp
        redaction_changed = False
        if redact is not None:
            try:
                redacted = redact(case)
            except Exception:
                raise ReplayError("redaction callback failed") from None
            _validate_redacted(original, redacted)
            effective = redacted
            redacted_fp = content_fingerprint("\x1f".join(_content_fingerprints(redacted)))
            redaction_changed = redacted_fp != content_fp

        try:
            raw = executor.execute(case, effective)
        except Exception:
            raw = RawExecutionResult(status="error", error_kind="executor_error")
        try:
            entry = _normalize_entry(raw, case.request_id, content_fp, redacted_fp, redaction_changed)
        except ReplayError:
            raise
        entries.append(entry)

    return ReplayArtifact(
        protocol=protocol,
        workload_hash=workload.workload_hash,
        size_bytes=workload.size_bytes,
        entries=entries,
    )
