"""Server-controlled, frozen benchmark profile specifications.

The profile is the *only* source of benchmark requests. Operators cannot
inject prompts, headers, or arbitrary HTTP parameters: every request body is
built from these fixed templates plus the endpoint's model name.

Hashes are deterministic (canonical JSON, sorted keys) so identical run inputs
yield identical protocol/workload fingerprints.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

PROFILE_VERSION = "serving-verdict.benchmark-profile.v1"
ARTIFACT_SCHEMA_VERSION = "serving-verdict.benchmark-run.v1"

UNMEASURABLE = "UNMEASURABLE"


@dataclass(frozen=True, slots=True)
class ArithmeticCase:
    case_id: str
    prompt: str
    expected: float


@dataclass(frozen=True, slots=True)
class ToolSchema:
    name: str
    description: str
    schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """One frozen request slot of a profile run.

    ``tool_schemata`` is None for every non-tool-call request.
    """

    request_id: str
    kind: str  # warmup | serial_fresh | serial_edit | concurrency | quality_arithmetic | quality_tool_call
    workload: str
    concurrency: int
    output_budget: int
    tool_schemata: tuple[ToolSchema, ...] | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "kind": self.kind,
            "workload": self.workload,
            "concurrency": self.concurrency,
            "output_budget": self.output_budget,
            "tool_schemata": [s.name for s in self.tool_schemata] if self.tool_schemata else None,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkProfile:
    name: str
    procedure_version: str
    warmup_count: int
    serial_fresh_count: int
    serial_edit_count: int
    concurrency_size: int
    concurrency_groups: int
    arithmetic_cases: tuple[ArithmeticCase, ...]
    tool_schemata: tuple[ToolSchema, ...]
    error_probe: dict[str, Any]
    request_budget_s: float
    group_budget_s: float

    # -- frozen content (server-controlled) ---------------------------------

    WARMUP_PROMPTS: tuple[str, ...] = (
        "Reply with the single word: warmup",
        "Reply with the single word: ready",
    )
    FRESH_PROMPTS: tuple[str, ...] = (
        "Write a Python function `slugify(text)` that lowercases text, replaces "
        "runs of whitespace with single hyphens, and strips leading/trailing "
        "hyphens. Return only the code, no prose.",
        "Write a Python function `clamp(value, lo, hi)` that returns lo when "
        "value < lo, hi when value > hi, else value. Return only the code.",
        "Write a Python function `median(values)` that returns the median of a "
        "non-empty list of numbers as a float. Return only the code.",
    )
    EDIT_PROMPTS: tuple[str, ...] = (
        "Rewrite the code below: change the function name `add` to `sum_two`, "
        "keep the behavior identical, and return only the rewritten code.\n\n"
        "def add(a, b):\n    return a + b",
        "Rewrite the code below: replace the hardcoded 42 with the parameter "
        "`default`, add a `default=42` keyword argument, keep behavior for "
        "explicit inputs, return only the rewritten code.\n\n"
        "def get_answer():\n    return 42",
        "Rewrite the code below: swap the two return branches so that empty "
        "input raises ValueError('empty'), return only the rewritten code.\n\n"
        "def first(items):\n    if items:\n        return items[0]\n    return None",
    )
    CONCURRENCY_PROMPTS: tuple[str, ...] = (
        "List three prime numbers greater than 100, one per line, nothing else.",
        "List three even numbers between 20 and 40, one per line, nothing else.",
        "List the first three positive perfect squares, one per line, nothing else.",
    )
    ARITHMETIC_INSTRUCTION = (
        "Answer with only the final integer, no units, no prose, no punctuation "
        "other than the digits and a leading minus sign if negative."
    )
    TOOL_INSTRUCTION = (
        "You MUST answer by calling the get_weather tool exactly once with the "
        "location 'Istanbul' and units 'metric'. Do not answer in plain text."
    )

    def request_specs(self) -> tuple[RequestSpec, ...]:
        specs: list[RequestSpec] = [
            RequestSpec(
                request_id=f"warmup-{i + 1}",
                kind="warmup",
                workload="warmup",
                concurrency=1,
                output_budget=64,
            )
            for i in range(self.warmup_count)
        ]
        specs += [
            RequestSpec(
                request_id=f"fresh-{i + 1}",
                kind="serial_fresh",
                workload="fresh_short",
                concurrency=1,
                output_budget=256,
            )
            for i in range(self.serial_fresh_count)
        ]
        specs += [
            RequestSpec(
                request_id=f"edit-{i + 1}",
                kind="serial_edit",
                workload="edit_repeat",
                concurrency=1,
                output_budget=256,
            )
            for i in range(self.serial_edit_count)
        ]
        for g in range(self.concurrency_groups):
            specs += [
                RequestSpec(
                    request_id=f"conc-{g + 1}-{i + 1}",
                    kind="concurrency",
                    workload="concurrency_3",
                    concurrency=self.concurrency_size,
                    output_budget=128,
                )
                for i in range(self.concurrency_size)
            ]
        specs += [
            RequestSpec(
                request_id=f"arith-{case.case_id}",
                kind="quality_arithmetic",
                workload="arithmetic",
                concurrency=1,
                output_budget=64,
            )
            for case in self.arithmetic_cases
        ]
        specs.append(
            RequestSpec(
                request_id=f"tool-call-{self.tool_schemata[0].name}",
                kind="quality_tool_call",
                workload="tool_call",
                concurrency=1,
                output_budget=512,
                tool_schemata=self.tool_schemata,
            )
        )
        return tuple(specs)

    def prompt_for(self, spec: RequestSpec, index_in_kind: int) -> str:
        if spec.kind == "warmup":
            return self.WARMUP_PROMPTS[index_in_kind % len(self.WARMUP_PROMPTS)]
        if spec.kind == "serial_fresh":
            return self.FRESH_PROMPTS[index_in_kind % len(self.FRESH_PROMPTS)]
        if spec.kind == "serial_edit":
            return self.EDIT_PROMPTS[index_in_kind % len(self.EDIT_PROMPTS)]
        if spec.kind == "concurrency":
            return self.CONCURRENCY_PROMPTS[index_in_kind % len(self.CONCURRENCY_PROMPTS)]
        if spec.kind == "quality_arithmetic":
            for case in self.arithmetic_cases:
                if spec.request_id == f"arith-{case.case_id}":
                    return f"{case.prompt}\n\n{self.ARITHMETIC_INSTRUCTION}"
            raise LookupError(f"no arithmetic case for {spec.request_id}")
        if spec.kind == "quality_tool_call":
            return self.TOOL_INSTRUCTION
        raise LookupError(f"unknown request kind: {spec.kind}")

    # -- canonical identities ------------------------------------------------

    def protocol_spec(self) -> dict[str, Any]:
        """Protocol identity: procedure shape, budgets, budgets-per-request,
        arithmetic expectations and tool schema. Deliberately excludes prompts
        of nothing operator-controlled — everything here is server-frozen."""
        return {
            "profile_version": PROFILE_VERSION,
            "name": self.name,
            "procedure_version": self.procedure_version,
            "warmup_count": self.warmup_count,
            "serial_fresh_count": self.serial_fresh_count,
            "serial_edit_count": self.serial_edit_count,
            "concurrency_size": self.concurrency_size,
            "concurrency_groups": self.concurrency_groups,
            "request_budget_s": self.request_budget_s,
            "group_budget_s": self.group_budget_s,
            "arithmetic": [
                {"case_id": c.case_id, "expected": c.expected} for c in self.arithmetic_cases
            ],
            "tool_schemata": [
                {"name": s.name, "description": s.description, "schema": s.schema}
                for s in self.tool_schemata
            ],
            "error_probe": self.error_probe,
            "streaming": {"protocol": "openai-sse", "usage_source": "api_usage_only"},
        }

    def workload_spec(self) -> dict[str, Any]:
        """Workload identity: the exact frozen request templates."""
        return {
            "profile_version": PROFILE_VERSION,
            "name": self.name,
            "procedure_version": self.procedure_version,
            "requests": [spec.to_public_dict() for spec in self.request_specs()],
            "prompts": {
                "warmup": list(self.WARMUP_PROMPTS),
                "serial_fresh": list(self.FRESH_PROMPTS),
                "serial_edit": list(self.EDIT_PROMPTS),
                "concurrency": list(self.CONCURRENCY_PROMPTS),
                "arithmetic": [
                    {"case_id": c.case_id, "prompt": c.prompt, "expected": c.expected}
                    for c in self.arithmetic_cases
                ],
                "tool_instruction": self.TOOL_INSTRUCTION,
                "arithmetic_instruction": self.ARITHMETIC_INSTRUCTION,
            },
        }


def _sha256_hex(spec: dict[str, Any]) -> str:
    canonical = json.dumps(
        spec, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def protocol_hash_from_spec(spec: dict[str, Any]) -> str:
    return _sha256_hex(spec)


def workload_hash_from_spec(spec: dict[str, Any]) -> str:
    return _sha256_hex(spec)


def protocol_hash(profile: BenchmarkProfile) -> str:
    return protocol_hash_from_spec(profile.protocol_spec())


def workload_hash(profile: BenchmarkProfile) -> str:
    return workload_hash_from_spec(profile.workload_spec())


def build_request_payload(profile: BenchmarkProfile, spec: RequestSpec, model: str) -> dict[str, Any]:
    """Build the fixed OpenAI-compatible chat-completions body for one spec.

    Contains no credentials. ``temperature`` is pinned to 0 and streaming is
    always on so TTFT/decode measurement is available whenever the endpoint
    reports usage.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply exactly: PROBE"}],
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": spec.output_budget,
    }
    if spec.kind == "quality_tool_call" and spec.tool_schemata:
        schema = spec.tool_schemata[0]
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": schema.name,
                    "description": schema.description,
                    "parameters": schema.schema,
                },
            }
        ]
        payload["tool_choice"] = {"type": "function", "function": {"name": schema.name}}
    return payload


_WEATHER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "location": {"type": "string", "description": "City name"},
        "units": {"type": "string", "enum": ["metric", "imperial"]},
    },
    "required": ["location", "units"],
    "additionalProperties": False,
}

QUICK_PROFILE = BenchmarkProfile(
    name="quick",
    procedure_version="quick-v1",
    warmup_count=2,
    serial_fresh_count=3,
    serial_edit_count=3,
    concurrency_size=3,
    concurrency_groups=1,
    arithmetic_cases=(
        ArithmeticCase("arith-1", "What is 7 + 10?", 17.0),
        ArithmeticCase("arith-2", "What is 15 - 7?", 8.0),
        ArithmeticCase("arith-3", "What is 14 * 9?", 126.0),
        ArithmeticCase("arith-4", "What is 12 * 3 + 6?", 42.0),
        ArithmeticCase("arith-5", "What is (3 + 3) / 2?", 3.0),
    ),
    tool_schemata=(
        ToolSchema(
            name="get_weather",
            description="Get the current weather for a location.",
            schema=_WEATHER_SCHEMA,
        ),
    ),
    error_probe={
        "kind": "invalid_request",
        "expect": "rejected",
        # A syntactically valid key that no endpoint will accept as the real key.
        "api_key_suffix": "-invalid-probe",
    },
    request_budget_s=120.0,
    group_budget_s=300.0,
)

_PROFILES: dict[str, BenchmarkProfile] = {"quick": QUICK_PROFILE}


def get_profile(name: str) -> BenchmarkProfile:
    try:
        return _PROFILES[name]
    except KeyError:
        raise LookupError(
            f"unknown benchmark profile: {name!r} (available: {', '.join(sorted(_PROFILES))})"
        ) from None
