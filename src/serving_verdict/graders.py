"""Deterministic quality-lite graders.

- Arithmetic: the response must normalize to exactly one integer-valued
  number matching the frozen expected value. Extra words, punctuation other
  than the numeric token, comma grouping, or non-integer values fail.
- Tool calling: the stream must contain exactly one call whose name and
  arguments validate against the frozen schema (types, required fields,
  enum values, no additional properties).

No LLM judgment, no approximate matching.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from serving_verdict.profile import QUICK_PROFILE, ToolSchema

# The grading schema MUST be the profile's frozen tool schema (identity is
# enforced by tests/test_graders.py). WEATHER_TOOL is that schema.
WEATHER_TOOL: ToolSchema = QUICK_PROFILE.tool_schemata[0]

# A single numeric token: optional sign, digits with at most one dot, optional
# fraction. No exponent notation, no comma grouping, no surrounding text.
_NUMBER_RE = re.compile(
    r"^[+-]?(?P<frac>\d+/\d+|\d+(?:\.\d*)?|\.\d+)$"
)


@dataclass(frozen=True, slots=True)
class ArithmeticGrade:
    case_id: str
    prompt: str
    normalized: str
    expected: float
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "prompt": self.prompt,
            "normalized": self.normalized,
            "expected": self.expected,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class ToolCallGrade:
    case_id: str
    tool_name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "tool_name": self.tool_name,
            "passed": self.passed,
            "detail": self.detail,
        }


def _normalize_number(text: str) -> str | None:
    """Return the canonical numeric string, or None if ``text`` is not exactly
    one numeric token."""
    stripped = text.strip()
    if not stripped:
        return ""
    match = _NUMBER_RE.match(stripped)
    if not match:
        return None
    value = stripped
    # canonical form: strip leading '+' and insignificant trailing '.0'
    value = value.lstrip("+")
    return value


def _numeric_value(token: str) -> float | None:
    try:
        if "/" in token:
            num, den = token.split("/")
            if int(den) == 0:
                return None
            return int(num) / int(den)
        return float(token)
    except (ValueError, ZeroDivisionError):
        return None


def grade_arithmetic(case_id: str, prompt: str, text: str, *, expected: float) -> ArithmeticGrade:
    """Grade one arithmetic response against the exact expected value."""
    token = _normalize_number(text)
    if token is None:
        # response is not a single numeric token -> fail, report raw (trimmed)
        normalized = text.strip()
        return ArithmeticGrade(
            case_id=case_id, prompt=prompt, normalized=normalized, expected=expected, passed=False
        )
    value = _numeric_value(token)
    if value is None:
        return ArithmeticGrade(
            case_id=case_id, prompt=prompt, normalized=token, expected=expected, passed=False
        )
    # exact integer-valued match: the expected value must be integral and the
    # normalized value must equal it exactly (no float tolerance).
    passed = expected == int(expected) and value == expected and value == int(value)
    return ArithmeticGrade(
        case_id=case_id, prompt=prompt, normalized=token, expected=expected, passed=passed
    )


def _validate_against_schema(args: Any, schema: dict[str, Any]) -> str | None:
    """Return None if valid, else a human detail string."""
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    props = schema.get("properties", {})
    required = schema.get("required", [])
    if schema.get("additionalProperties") is False:
        extra = set(args) - set(props)
        if extra:
            return f"additional properties not allowed: {sorted(extra)}"
    for field in required:
        if field not in args:
            return f"missing required field: {field}"
    for field, value in args.items():
        spec = props.get(field)
        if spec is None:
            continue  # covered by additionalProperties above
        error = _check_value(value, spec)
        if error:
            return f"field {field!r}: {error}"
    return None


def _check_value(value: Any, spec: dict[str, Any]) -> str | None:
    expected_type = spec.get("type")
    if expected_type == "string" and not isinstance(value, str):
        return "expected string"
    if expected_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        return "expected integer"
    if expected_type == "number" and (
        isinstance(value, bool) or not isinstance(value, (int, float))
    ):
        return "expected number"
    if expected_type == "boolean" and not isinstance(value, bool):
        return "expected boolean"
    enum = spec.get("enum")
    if enum is not None and value not in enum:
        return f"value {value!r} not in enum {enum}"
    return None


def grade_tool_call(tool: ToolSchema, *, tool_calls: list[dict[str, str]]) -> ToolCallGrade:
    """Grade the streamed tool calls against the frozen tool schema.

    Exactly one call is required; its name and arguments must validate.
    """
    case_id = f"tool-call-{tool.name}"
    if len(tool_calls) != 1:
        return ToolCallGrade(
            case_id=case_id,
            tool_name=tool.name,
            passed=False,
            detail=f"expected exactly 1 tool call, got {len(tool_calls)}",
        )
    call = tool_calls[0]
    if call.get("name") != tool.name:
        return ToolCallGrade(
            case_id=case_id,
            tool_name=tool.name,
            passed=False,
            detail=f"wrong tool name: {call.get('name')!r}",
        )
    try:
        args = json.loads(call.get("arguments") or "")
    except json.JSONDecodeError as exc:
        return ToolCallGrade(
            case_id=case_id,
            tool_name=tool.name,
            passed=False,
            detail=f"arguments are not valid JSON: {exc.msg}",
        )
    error = _validate_against_schema(args, tool.schema)
    if error:
        return ToolCallGrade(
            case_id=case_id, tool_name=tool.name, passed=False, detail=error
        )
    return ToolCallGrade(
        case_id=case_id, tool_name=tool.name, passed=True, detail="valid tool call"
    )
