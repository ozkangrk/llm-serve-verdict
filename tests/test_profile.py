"""Server-controlled frozen `quick` benchmark profile contracts."""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from serving_verdict.profile import (
    QUICK_PROFILE,
    get_profile,
    protocol_hash,
    protocol_hash_from_spec,
    workload_hash,
    workload_hash_from_spec,
)


def specs() -> list:
    return list(QUICK_PROFILE.request_specs())


def test_quick_profile_shape_is_frozen() -> None:
    assert QUICK_PROFILE.name == "quick"
    assert QUICK_PROFILE.procedure_version == "quick-v1"
    assert QUICK_PROFILE.warmup_count == 2
    assert QUICK_PROFILE.serial_fresh_count == 3
    assert QUICK_PROFILE.serial_edit_count == 3
    assert QUICK_PROFILE.concurrency_size == 3
    assert len(QUICK_PROFILE.arithmetic_cases) == 5
    # quick profile runs exactly one concurrency group of size 3
    assert QUICK_PROFILE.concurrency_groups == 1
    # immutability
    with pytest.raises(FrozenInstanceError):
        QUICK_PROFILE.warmup_count = 99  # type: ignore[misc]


def test_get_profile_resolves_quick_and_rejects_unknown() -> None:
    assert get_profile("quick") is QUICK_PROFILE
    with pytest.raises(LookupError):
        get_profile("standard")
    with pytest.raises(LookupError):
        get_profile("replay")


def test_request_spec_inventory_is_frozen() -> None:
    by_kind: dict[str, int] = {}
    for spec in specs():
        by_kind[spec.kind] = by_kind.get(spec.kind, 0) + 1
    assert by_kind == {
        "warmup": 2,
        "serial_fresh": 3,
        "serial_edit": 3,
        "concurrency": 3,
        "quality_arithmetic": 5,
        "quality_tool_call": 1,
    }
    # warmups excluded: 15 measured requests
    measured = [s for s in specs() if s.kind != "warmup"]
    assert len(measured) == 15
    # request ids are unique
    ids = [s.request_id for s in specs()]
    assert len(ids) == len(set(ids))


def test_arithmetic_cases_are_frozen() -> None:
    answers = [c.expected for c in QUICK_PROFILE.arithmetic_cases]
    assert answers == [17, 8, 126, 42, 3]
    for case in QUICK_PROFILE.arithmetic_cases:
        assert isinstance(case.case_id, str) and case.case_id
        assert isinstance(case.prompt, str) and case.prompt
        assert isinstance(case.expected, float)


def test_protocol_hash_is_deterministic_and_hex64() -> None:
    h1 = protocol_hash(QUICK_PROFILE)
    h2 = protocol_hash(QUICK_PROFILE)
    assert h1 == h2
    assert len(h1) == 64
    int(h1, 16)  # raises if not hex


def test_protocol_hash_is_sensitive_to_spec_shape() -> None:
    doc1 = QUICK_PROFILE.protocol_spec()
    doc2 = json.loads(json.dumps(doc1))
    doc2["warmup_count"] = 3
    base = protocol_hash(QUICK_PROFILE)
    assert protocol_hash_from_spec(doc1) == base
    assert protocol_hash_from_spec(doc2) != base


def test_workload_hash_covers_all_request_templates() -> None:
    w1 = workload_hash(QUICK_PROFILE)
    w2 = workload_hash(QUICK_PROFILE)
    assert w1 == w2
    assert len(w1) == 64
    int(w1, 16)
    doc = QUICK_PROFILE.workload_spec()
    assert workload_hash_from_spec(doc) == w1
    # workload hash differs from protocol hash (different identities)
    assert w1 != protocol_hash(QUICK_PROFILE)
    # changing a frozen prompt changes the workload hash
    doc2 = json.loads(json.dumps(doc))
    doc2["prompts"]["serial_fresh"][0] = doc2["prompts"]["serial_fresh"][0] + " changed"
    assert workload_hash_from_spec(doc2) != w1


def test_prompts_are_frozen_per_kind() -> None:
    fresh = [QUICK_PROFILE.prompt_for(s, i) for i, s in enumerate(specs()) if s.kind == "serial_fresh"]
    assert len(fresh) == 3
    assert all(isinstance(p, str) and p for p in fresh)
    assert len(set(fresh)) == 3  # distinct frozen prompts
    arithmetic = [
        QUICK_PROFILE.prompt_for(s, 0)
        for s in specs()
        if s.kind == "quality_arithmetic"
    ]
    assert any("7 + 10" in p for p in arithmetic)


def test_request_payload_is_frozen_and_secret_free() -> None:
    from serving_verdict.profile import build_request_payload

    for spec in specs():
        payload = build_request_payload(QUICK_PROFILE, spec, model="test-model")
        assert payload["model"] == "test-model"
        assert payload["temperature"] == 0
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["max_tokens"] == spec.output_budget
        messages = payload["messages"]
        assert len(messages) >= 1
        assert messages[0]["role"] == "user"
        assert isinstance(messages[0]["content"], str) and messages[0]["content"]
        dumped = json.dumps(payload)
        assert "api_key" not in dumped
        assert "secret" not in dumped


def test_request_payload_uses_the_frozen_kind_prompt() -> None:
    from serving_verdict.profile import build_request_payload

    fresh = [spec for spec in specs() if spec.kind == "serial_fresh"]
    first = build_request_payload(QUICK_PROFILE, fresh[0], "m", index_in_kind=0)
    second = build_request_payload(QUICK_PROFILE, fresh[1], "m", index_in_kind=1)
    assert first["messages"][0]["content"] == QUICK_PROFILE.prompt_for(fresh[0], 0)
    assert second["messages"][0]["content"] == QUICK_PROFILE.prompt_for(fresh[1], 1)
    assert first["messages"] != second["messages"]


def test_tool_call_payload_carries_frozen_schema() -> None:
    from serving_verdict.profile import build_request_payload

    spec = [s for s in specs() if s.kind == "quality_tool_call"][0]
    payload = build_request_payload(QUICK_PROFILE, spec, model="test-model")
    assert "tools" in payload
    tools = payload["tools"]
    assert len(tools) == 1
    assert tools[0]["type"] == "function"
    fn = tools[0]["function"]
    assert fn["name"] == "get_weather"
    props = fn["parameters"]["properties"]
    assert set(props) == {"location", "units"}
    assert fn["parameters"]["required"] == ["location", "units"]
    assert fn["parameters"]["additionalProperties"] is False
    # tool_choice pins the call to the frozen schema
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}
    # non-tool requests must never carry tools
    for s in specs():
        if s.kind != "quality_tool_call":
            assert "tools" not in build_request_payload(QUICK_PROFILE, s, model="m")


def test_error_probe_spec_is_fixed() -> None:
    probe = QUICK_PROFILE.error_probe
    assert probe["kind"] == "invalid_request"
    assert probe["expect"] == "rejected"
    # the probe mutates the api key so no endpoint can treat it as a valid auth
    assert probe["api_key_suffix"] == "-invalid-probe"
