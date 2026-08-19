"""Deterministic quality-lite graders: arithmetic exactness + tool calling.

These are *machine* graders: no LLM judgment, no float tolerance on exact
integer arithmetic, and the tool schema is enforced field-by-field.
"""
from __future__ import annotations

import json

import pytest

from serving_verdict.graders import (
    WEATHER_TOOL,
    grade_arithmetic,
    grade_tool_call,
)
from serving_verdict.profile import QUICK_PROFILE


def test_grader_tool_schema_matches_profile_frozen_schema() -> None:
    # fail-closed drift guard: the grading schema MUST be the profile schema
    assert QUICK_PROFILE.tool_schemata == (WEATHER_TOOL,)

CASE_17 = "What is 7 + 10?"


def test_arithmetic_exact_pass() -> None:
    r = grade_arithmetic("arith-1", CASE_17, "17", expected=17.0)
    assert r.passed is True
    assert r.case_id == "arith-1"
    assert r.normalized == "17"


@pytest.mark.parametrize("text", ["17.", "17.0", " 17 ", "+17"])
def test_arithmetic_numeric_equivalent_pass(text: str) -> None:
    r = grade_arithmetic("arith-1", CASE_17, text, expected=17.0)
    assert r.passed is True


def test_arithmetic_wrong_answer_fails() -> None:
    r = grade_arithmetic("arith-1", CASE_17, "18", expected=17.0)
    assert r.passed is False
    assert r.normalized == "18"


def test_arithmetic_non_integer_answer_fails() -> None:
    # exact arithmetic: 7+10 is an integer; 17.5 is a wrong answer
    r = grade_arithmetic("arith-1", CASE_17, "17.5", expected=17.0)
    assert r.passed is False


def test_arithmetic_fraction_not_accepted() -> None:
    # 34/2 normalizes to 17 -> pass (it IS the exact value)
    r = grade_arithmetic("arith-1", CASE_17, "34/2", expected=17.0)
    assert r.passed is True
    r2 = grade_arithmetic("arith-1", CASE_17, "16/1", expected=17.0)
    assert r2.passed is False


def test_arithmetic_rejects_extra_words() -> None:
    r = grade_arithmetic("arith-1", CASE_17, "the answer is 17", expected=17.0)
    assert r.passed is False
    r2 = grade_arithmetic("arith-1", CASE_17, "17 (I think)", expected=17.0)
    assert r2.passed is False


def test_arithmetic_rejects_comma_grouping() -> None:
    r = grade_arithmetic("arith-x", "What is 1000000 + 1?", "1,000,001", expected=1000001.0)
    assert r.passed is False


def test_arithmetic_empty_response_fails() -> None:
    r = grade_arithmetic("arith-1", CASE_17, "", expected=17.0)
    assert r.passed is False
    assert r.normalized == ""


def test_arithmetic_multiple_numbers_rejected() -> None:
    r = grade_arithmetic("arith-1", CASE_17, "7 + 10 = 17", expected=17.0)
    assert r.passed is False


def test_arithmetic_negative_expected() -> None:
    r = grade_arithmetic("arith-neg", "What is 3 - 10?", "-7", expected=-7.0)
    assert r.passed is True
    r2 = grade_arithmetic("arith-neg", "What is 3 - 10?", "7", expected=-7.0)
    assert r2.passed is False


def test_tool_call_pass_exact_schema() -> None:
    args = {"location": "Istanbul", "units": "metric"}
    r = grade_tool_call(
        WEATHER_TOOL,
        tool_calls=[{"id": "c1", "name": "get_weather", "arguments": json.dumps(args)}],
    )
    assert r.passed is True
    # case id is derived from the frozen tool name (matches the profile request)
    assert r.case_id == "tool-call-get_weather"


def test_tool_call_missing_tool_fails() -> None:
    r = grade_tool_call(WEATHER_TOOL, tool_calls=[])
    assert r.passed is False


def test_tool_call_wrong_name_fails() -> None:
    args = {"location": "Istanbul", "units": "metric"}
    r = grade_tool_call(
        WEATHER_TOOL,
        tool_calls=[{"id": "c1", "name": "set_weather", "arguments": json.dumps(args)}],
    )
    assert r.passed is False


def test_tool_call_invalid_json_arguments_fails() -> None:
    r = grade_tool_call(
        WEATHER_TOOL,
        tool_calls=[{"id": "c1", "name": "get_weather", "arguments": "{not json"}],
    )
    assert r.passed is False


def test_tool_call_missing_required_field_fails() -> None:
    args = {"location": "Istanbul"}
    r = grade_tool_call(
        WEATHER_TOOL,
        tool_calls=[{"id": "c1", "name": "get_weather", "arguments": json.dumps(args)}],
    )
    assert r.passed is False


def test_tool_call_wrong_type_fails() -> None:
    args = {"location": 42, "units": "metric"}
    r = grade_tool_call(
        WEATHER_TOOL,
        tool_calls=[{"id": "c1", "name": "get_weather", "arguments": json.dumps(args)}],
    )
    assert r.passed is False


def test_tool_call_enum_violation_fails() -> None:
    args = {"location": "Istanbul", "units": "furlongs"}
    r = grade_tool_call(
        WEATHER_TOOL,
        tool_calls=[{"id": "c1", "name": "get_weather", "arguments": json.dumps(args)}],
    )
    assert r.passed is False


def test_tool_call_additional_properties_rejected() -> None:
    args = {"location": "Istanbul", "units": "metric", "extra": 1}
    r = grade_tool_call(
        WEATHER_TOOL,
        tool_calls=[{"id": "c1", "name": "get_weather", "arguments": json.dumps(args)}],
    )
    assert r.passed is False


def test_tool_call_non_object_arguments_fails() -> None:
    r = grade_tool_call(
        WEATHER_TOOL,
        tool_calls=[{"id": "c1", "name": "get_weather", "arguments": "[1,2]"}],
    )
    assert r.passed is False


def test_tool_call_multiple_calls_fails() -> None:
    args = {"location": "Istanbul", "units": "metric"}
    r = grade_tool_call(
        WEATHER_TOOL,
        tool_calls=[
            {"id": "c1", "name": "get_weather", "arguments": json.dumps(args)},
            {"id": "c2", "name": "get_weather", "arguments": json.dumps(args)},
        ],
    )
    assert r.passed is False


def test_grader_results_are_serializable_and_secret_free() -> None:
    r1 = grade_arithmetic("arith-1", CASE_17, "17", expected=17.0)
    r2 = grade_tool_call(WEATHER_TOOL, tool_calls=[])
    blob = json.dumps({"a": r1.to_dict(), "t": r2.to_dict()})
    assert "api_key" not in blob
    assert "authorization" not in blob.lower()
    assert r1.to_dict()["passed"] is True
    assert r2.to_dict()["passed"] is False
    # type sanity
    assert isinstance(r1, object)
    assert isinstance(r2, object)
