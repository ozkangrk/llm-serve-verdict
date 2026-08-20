"""Fail-closed adapter contracts for external benchmark evidence."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class AdapterError(ValueError):
    """Base adapter contract error; messages must not contain source values."""


class InvalidAdapterPayload(AdapterError):
    """A detected source is malformed or lacks required semantics."""


class UnsupportedAdapterSchema(AdapterError):
    """No supported adapter/schema can authoritatively parse the source."""


@dataclass(frozen=True, slots=True)
class DetectionResult:
    matched: bool
    adapter_id: str
    reason: str
    schema_version: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    source_versions: tuple[str, ...]
    metric_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    compatibility_status: str


@dataclass(frozen=True, slots=True)
class MetricSample:
    metric_id: str
    value: float
    unit: str
    direction: str
    procedure_version: str
    aggregation: str
    dimensions: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class NormalizedEvidence:
    adapter_id: str
    source_schema_version: str
    source_sha256: str
    metrics: tuple[MetricSample, ...]
    provenance: tuple[tuple[str, str], ...] = ()

    @classmethod
    def empty(cls, adapter_id: str, source_sha256: str) -> NormalizedEvidence:
        return cls(adapter_id, "unknown", source_sha256, ())

    def metric(self, metric_id: str) -> MetricSample:
        matches = [metric for metric in self.metrics if metric.metric_id == metric_id]
        if len(matches) != 1:
            raise KeyError(metric_id)
        return matches[0]


@runtime_checkable
class EvidenceAdapter(Protocol):
    adapter_id: str
    capabilities: AdapterCapabilities

    def detect(self, artifact: object) -> DetectionResult: ...

    def parse(self, artifact: object, *, source_sha256: str) -> NormalizedEvidence: ...


class AdapterRegistry:
    """Immutable adapter registry with strict single-match detection."""

    def __init__(self, adapters: tuple[EvidenceAdapter, ...]) -> None:
        by_id: dict[str, EvidenceAdapter] = {}
        for adapter in adapters:
            adapter_id = getattr(adapter, "adapter_id", "")
            if not isinstance(adapter_id, str) or not adapter_id:
                raise AdapterError("adapter_id must be a non-empty string")
            if adapter_id in by_id:
                raise AdapterError(f"duplicate adapter_id: {adapter_id}")
            by_id[adapter_id] = adapter
        self._adapters: Mapping[str, EvidenceAdapter] = MappingProxyType(
            dict(sorted(by_id.items()))
        )

    @property
    def adapters(self) -> Mapping[str, EvidenceAdapter]:
        return self._adapters

    @property
    def adapter_ids(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def detect_one(self, artifact: object) -> EvidenceAdapter:
        matches: list[EvidenceAdapter] = []
        for adapter in self._adapters.values():
            try:
                result = adapter.detect(artifact)
            except Exception:
                continue
            if result.matched:
                matches.append(adapter)
        if not matches:
            raise UnsupportedAdapterSchema("unsupported adapter schema")
        if len(matches) != 1:
            raise AdapterError(
                "ambiguous adapter detection: "
                + ", ".join(sorted(adapter.adapter_id for adapter in matches))
            )
        return matches[0]

    def parse_one(self, artifact: object, *, source_sha256: str) -> NormalizedEvidence:
        if not isinstance(source_sha256, str) or not _SHA_RE.fullmatch(source_sha256):
            raise InvalidAdapterPayload("source_sha256 must be 64 lowercase hex characters")
        return self.detect_one(artifact).parse(
            artifact, source_sha256=source_sha256
        )
