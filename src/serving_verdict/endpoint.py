"""Credential-safe endpoint configuration for benchmark targets."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

ENDPOINT_SCHEMA_VERSION = "serving-verdict.endpoint.v1"
_ALLOWED_FIELDS = {"schema_version", "id", "base_url", "model", "api_key_env"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class EndpointConfigError(ValueError):
    """Endpoint configuration is unsafe, malformed, or incomplete."""


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    """Public endpoint metadata. Secret values are deliberately not fields."""

    endpoint_id: str
    base_url: str
    model: str
    api_key_env: str
    remote: bool
    schema_version: str = ENDPOINT_SCHEMA_VERSION

    def public_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.endpoint_id,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "remote": self.remote,
        }


def _required_string(doc: dict[str, Any], key: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EndpointConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validated_url(raw: str) -> tuple[str, bool]:
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise EndpointConfigError("base_url is invalid") from exc
    if parsed.scheme not in {"http", "https"}:
        raise EndpointConfigError("base_url must use http or https")
    if not parsed.hostname:
        raise EndpointConfigError("base_url must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise EndpointConfigError("base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise EndpointConfigError("base_url must not contain query or fragment data")
    normalized_path = parsed.path.rstrip("/")
    normalized = urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, normalized_path, "", "")
    )
    return normalized, not _is_loopback(parsed.hostname)


def load_endpoint_config(
    path: str | Path, *, allow_remote: bool = False
) -> EndpointConfig:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EndpointConfigError("endpoint config could not be read") from exc
    if not isinstance(raw, dict):
        raise EndpointConfigError("endpoint config must be a mapping")
    doc: dict[str, Any] = raw
    unknown = sorted(set(doc) - _ALLOWED_FIELDS)
    if unknown:
        raise EndpointConfigError(f"unknown fields: {', '.join(unknown)}")
    if doc.get("schema_version") != ENDPOINT_SCHEMA_VERSION:
        raise EndpointConfigError(
            f"schema_version must be {ENDPOINT_SCHEMA_VERSION}"
        )
    endpoint_id = _required_string(doc, "id")
    if not _ID_RE.fullmatch(endpoint_id):
        raise EndpointConfigError("id contains unsupported characters")
    model = _required_string(doc, "model")
    api_key_env = _required_string(doc, "api_key_env")
    if not _ENV_RE.fullmatch(api_key_env):
        raise EndpointConfigError("api_key_env must be an environment variable name")
    base_url, remote = _validated_url(_required_string(doc, "base_url"))
    if remote and not allow_remote:
        raise EndpointConfigError(
            "remote endpoint requires explicit allow_remote acknowledgement"
        )
    return EndpointConfig(
        endpoint_id=endpoint_id,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        remote=remote,
    )


def resolve_api_key(config: EndpointConfig) -> str:
    value = os.environ.get(config.api_key_env)
    if value is None or not value:
        raise EndpointConfigError("endpoint API key environment variable is not set")
    return value
