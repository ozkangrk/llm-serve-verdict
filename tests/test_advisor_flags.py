"""Unsafe/incompatible flag detection and fail-closed behavior."""
from __future__ import annotations

import pytest
from helpers_advisor import raw_advisor_doc

from serving_verdict.advisor import AdvisorError, advise


def test_unsafe_flag_fails_closed():
    doc = raw_advisor_doc(
        current_flags={
            "allowed_max_tokens": 32768,
            "trust_remote_code": True,
            "download_dir": "/data",
        }
    )
    with pytest.raises(AdvisorError) as exc:
        advise(doc)
    assert "trust_remote_code" in str(exc.value)
    assert "incompatible" in str(exc.value).lower() or "unsafe" in str(exc.value).lower()


def test_incompatible_flag_pair_fails_closed():
    doc = raw_advisor_doc(
        current_flags={
            "enable_chunked_prefill": True,
            "disable_chunked_prefill": True,
        }
    )
    with pytest.raises(AdvisorError):
        advise(doc)


def test_unknown_flag_fails_closed():
    doc = raw_advisor_doc(current_flags={"totally_made_up_flag": 1})
    with pytest.raises(AdvisorError):
        advise(doc)


def test_bad_flag_type_fails_closed():
    doc = raw_advisor_doc(current_flags={"allowed_max_tokens": "32768"})
    with pytest.raises(AdvisorError):
        advise(doc)


def test_out_of_range_flag_fails_closed():
    doc = raw_advisor_doc(current_flags={"allowed_max_tokens": 3})
    with pytest.raises(AdvisorError):
        advise(doc)


def test_secret_in_current_flags_fails_closed():
    doc = raw_advisor_doc(current_flags={"api_key": "sk-supersecret123"})
    with pytest.raises(AdvisorError):
        advise(doc)


def test_recipe_refused_when_unsafe_flags_present():
    """Even if the caller bypasses advise(), the recipe builder must refuse."""
    from serving_verdict.advisor.recipe import build_recipe

    with pytest.raises(AdvisorError):
        build_recipe(
            family="vllm",
            model_path="org/model-7b",
            current_flags={"trust_remote_code": True},
            overrides={},
        )
