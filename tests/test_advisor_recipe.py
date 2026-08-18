"""Recipe generation: inert launch argv, rollback, shlex renderer."""
from __future__ import annotations

import shlex
from typing import Any

import pytest
from helpers_advisor import raw_advisor_doc

from serving_verdict.advisor import AdvisorError, advise, recipe


def _recipe(family: str = "vllm", flags: dict[str, Any] | None = None, **bench: Any) -> Any:
    doc = raw_advisor_doc(family=family, current_flags=flags or {})
    if bench:
        doc["benchmark"] = {**doc["benchmark"], **bench}
    result = advise(doc)
    assert result.recipe is not None
    return result.recipe


def test_vllm_launch_argv_is_inert_and_replayable():
    r = _recipe("vllm", {"allowed_max_tokens": 32768, "max_num_batched_tokens": 8192},
                decode_tokens_per_s=20.0)
    assert isinstance(r.launch_argv, list)
    assert all(isinstance(t, str) for t in r.launch_argv)
    assert r.launch_argv[0] == "python"
    assert "-m" in r.launch_argv and r.launch_argv[r.launch_argv.index("-m") + 1] == "vllm.entrypoints.openai.api_server"
    assert "org/model-7b" in r.launch_argv
    assert "--allowed-max-tokens" in r.launch_argv
    # Top rule (THROUGHPUT_LOW) doubles allowed_max_tokens 32768 -> 65536 in the
    # proposed launch; the unchanged current value is preserved in the rollback.
    assert "65536" in r.launch_argv
    assert "32768" not in r.launch_argv
    # Round-trips cleanly through shlex.
    assert shlex.split(shlex.join(r.launch_argv)) == r.launch_argv


def test_sglang_and_llamacpp_profiles():
    r = _recipe("sglang", {"context_length": 8192}, decode_tokens_per_s=20.0)
    assert "-m" in r.launch_argv
    assert r.launch_argv[r.launch_argv.index("-m") + 1] == "sglang.launch_server"
    assert "--context-length" in r.launch_argv
    r2 = _recipe("llama.cpp", {"ctx_size": 4096}, decode_tokens_per_s=20.0)
    assert r2.launch_argv[0] == "llama-server"
    assert "--ctx-size" in r2.launch_argv
    assert "4096" in r2.launch_argv


def test_renderer_is_shell_escaped_and_never_executed():
    r = _recipe("vllm", {"allowed_max_tokens": 32768}, decode_tokens_per_s=20.0)
    text = r.rendered_shell
    assert isinstance(text, str)
    assert text == shlex.join(r.launch_argv)
    assert "&&" not in text and ";" not in text and "$(" not in text and "`" not in text
    rb = r.rendered_rollback_shell
    assert rb == shlex.join(r.rollback_argv)


def test_exact_rollback_restores_current_state():
    """Rollback argv == profile base + current flags (unchanged)."""
    flags = {"allowed_max_tokens": 32768, "max_num_batched_tokens": 8192}
    doc = raw_advisor_doc(current_flags=flags, benchmark={"decode_tokens_per_s": 20.0})
    result = advise(doc)
    r = result.recipe
    assert r is not None
    assert r.rollback_argv[0] == "python"
    assert "--allowed-max-tokens" in r.rollback_argv
    assert "32768" in r.rollback_argv
    # No override appears in the rollback (rollback is the unchanged current state).
    assert "--allowed-max-tokens 65536" not in r.rendered_rollback_shell
    assert "65536" not in r.rendered_rollback_shell
    # ...but it IS in the proposed launch.
    assert "--allowed-max-tokens 65536" in r.rendered_shell
    # Current value is preserved in the rollback.
    assert "32768" in r.rendered_rollback_shell
    # Diff is non-empty exactly for the overridden flags, in stable order.
    assert r.flag_diff
    diff_names = [d.flag for d in r.flag_diff]
    assert diff_names == sorted(diff_names)
    for d in r.flag_diff:
        assert d.before is not None
        assert d.after is not None
        assert d.before != d.after


def test_rollback_of_clean_config_is_identity():
    doc = raw_advisor_doc(current_flags={}, benchmark={"decode_tokens_per_s": 20.0})
    result = advise(doc)
    r = result.recipe
    assert r is not None
    assert r.flag_diff == ()
    # Without flags, the launch is the profile base; rollback is identical.
    assert r.rollback_argv == r.launch_argv


def test_launch_is_dry_run_only(monkeypatch: pytest.MonkeyPatch):
    """Nothing in the advisor may execute: exec/subprocess/spawn are patched to fail."""

    def _boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("advisor attempted to execute a command")

    import subprocess

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    import builtins

    monkeypatch.setattr(builtins, "eval", _boom)
    monkeypatch.setattr(builtins, "exec", _boom)
    import os

    for fn in ("execve", "execv", "execl", "execlp", "spawnl", "spawnv"):
        if hasattr(os, fn):
            monkeypatch.setattr(os, fn, _boom)

    doc = raw_advisor_doc(current_flags={}, benchmark={"decode_tokens_per_s": 20.0})
    result = advise(doc)
    assert result.recipe is not None
    assert result.digest.startswith("sha256:")


def test_model_path_rejects_traversal():
    doc = raw_advisor_doc(model_path="../outside/model", benchmark={"decode_tokens_per_s": 20.0})
    with pytest.raises(AdvisorError):
        advise(doc)


def test_model_path_rejects_shell_metacharacters():
    for bad in (
        "org/model; rm -rf /",
        "org/model && curl evil",
        "org/$(whoami)",
        "org/mo`del`",
        "org/mo'del",
        "org/mo\"del",
        "org/mo|del",
        "org/mo>out",
        "org/mo<in",
        "org/mo*del",
        "org/mo?del",
        "org/mo&del",
        "org/mo\ndel",
        "org/mo\tdel",
    ):
        doc = raw_advisor_doc(model_path=bad, benchmark={"decode_tokens_per_s": 20.0})
        with pytest.raises(AdvisorError):
            advise(doc)


def test_artifact_is_serializable_and_reverifies():
    doc = raw_advisor_doc(current_flags={"allowed_max_tokens": 32768},
                          benchmark={"decode_tokens_per_s": 20.0})
    result = advise(doc)
    art = recipe.artifact_to_dict(result)
    assert art["schema_version"].startswith("serving-verdict.advisor.")
    assert art["digest"] == result.digest
    # Re-verify against the payload (digest covers everything except created_at/digest).
    assert recipe.verify_artifact(art)["valid"] is True
    # Tampering breaks verification.
    art2 = {**art, "recommendations": []}
    with pytest.raises(AdvisorError):
        recipe.verify_artifact(art2)
