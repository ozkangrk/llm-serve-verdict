"""Endpoint configuration contracts for the built-in benchmark runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from serving_verdict.endpoint import (
    EndpointConfigError,
    load_endpoint_config,
    resolve_api_key,
)


def write_endpoint(tmp_path: Path, **overrides: object) -> Path:
    doc: dict[str, object] = {
        "schema_version": "serving-verdict.endpoint.v1",
        "id": "local-qwen",
        "base_url": "http://127.0.0.1:8888/v1",
        "model": "qwen-local",
        "api_key_env": "SERVING_VERDICT_API_KEY_LOCAL_QWEN",
    }
    doc.update(overrides)
    path = tmp_path / "endpoint.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def test_local_endpoint_loads_and_public_payload_has_no_secret(tmp_path: Path) -> None:
    config = load_endpoint_config(write_endpoint(tmp_path))
    assert config.endpoint_id == "local-qwen"
    assert config.base_url == "http://127.0.0.1:8888/v1"
    assert config.model == "qwen-local"
    assert config.api_key_env == "SERVING_VERDICT_API_KEY_LOCAL_QWEN"
    payload = config.public_payload()
    assert payload == {
        "schema_version": "serving-verdict.endpoint.v1",
        "id": "local-qwen",
        "base_url": "http://127.0.0.1:8888/v1",
        "model": "qwen-local",
        "api_key_env": "SERVING_VERDICT_API_KEY_LOCAL_QWEN",
        "remote": False,
    }
    assert "secret" not in json.dumps(payload).lower()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1/",
        "http://[::1]:8000/v1",
    ],
)
def test_loopback_variants_are_allowed_and_trailing_slash_is_removed(
    tmp_path: Path, url: str
) -> None:
    config = load_endpoint_config(write_endpoint(tmp_path, base_url=url))
    assert config.remote is False
    assert not config.base_url.endswith("/")


def test_remote_endpoint_requires_explicit_opt_in(tmp_path: Path) -> None:
    path = write_endpoint(tmp_path, base_url="https://models.example.com/v1")
    with pytest.raises(EndpointConfigError, match="allow_remote"):
        load_endpoint_config(path)
    config = load_endpoint_config(path, allow_remote=True)
    assert config.remote is True


@pytest.mark.parametrize(
    "url",
    [
        "http://user:password@127.0.0.1:8000/v1",
        "ftp://127.0.0.1/model",
        "http://127.0.0.1:8000/v1?api_key=secret",
        "http://127.0.0.1:8000/v1#secret",
    ],
)
def test_credentials_and_unsupported_url_shapes_are_rejected(
    tmp_path: Path, url: str
) -> None:
    with pytest.raises(EndpointConfigError):
        load_endpoint_config(write_endpoint(tmp_path, base_url=url))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "wrong"),
        ("id", ""),
        ("id", "not valid"),
        ("model", ""),
        ("api_key_env", ""),
        ("api_key_env", "BAD-NAME"),
    ],
)
def test_invalid_endpoint_fields_are_rejected(
    tmp_path: Path, field: str, value: str
) -> None:
    with pytest.raises(EndpointConfigError):
        load_endpoint_config(write_endpoint(tmp_path, **{field: value}))


def test_api_key_is_resolved_only_at_call_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_endpoint_config(write_endpoint(tmp_path))
    monkeypatch.setenv(config.api_key_env, "top-secret-value")
    assert resolve_api_key(config) == "top-secret-value"
    assert "top-secret-value" not in repr(config)
    assert "top-secret-value" not in json.dumps(config.public_payload())


def test_missing_api_key_environment_fails_without_leaking_name_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_endpoint_config(write_endpoint(tmp_path))
    monkeypatch.delenv(config.api_key_env, raising=False)
    with pytest.raises(EndpointConfigError, match="environment variable is not set") as exc:
        resolve_api_key(config)
    assert "top-secret" not in str(exc.value)


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(EndpointConfigError, match="unknown fields"):
        load_endpoint_config(write_endpoint(tmp_path, arbitrary_header="unsafe"))
