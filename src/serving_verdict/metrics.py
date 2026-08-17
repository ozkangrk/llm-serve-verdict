"""Metric semantic registry.

Every metric ID has fixed semantics. Two values are comparable only when the
metric ID, unit, procedure version, workload ID, concurrency, output budget,
thinking mode, warm/cold status and aggregation semantics all match. There is
no automatic conversion between decode / e2e / aggregate throughput.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

Direction = str  # "higher_better" | "lower_better"


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    unit: str
    direction: Direction
    procedure_version: str
    aggregation: str
    definition: str


@dataclass(frozen=True)
class MetricDimensions:
    """Full semantic comparability key for one metric value."""

    unit: str
    procedure_version: str
    workload_id: str
    concurrency: int
    output_budget: int
    thinking_mode: str
    warm_cold: str
    aggregation: str


@dataclass(frozen=True)
class MetricSample:
    metric_id: str
    value: float
    dimensions: MetricDimensions
    source_artifact: str | None = None


_DEFINITIONS: dict[str, MetricDefinition] = {
    "decode_tokens_per_s": MetricDefinition(
        metric_id="decode_tokens_per_s",
        unit="tok/s",
        direction="higher_better",
        procedure_version="v1",
        aggregation="median",
        definition="post-first-token decode rate, tok/s, higher is better",
    ),
    "e2e_output_tokens_per_s": MetricDefinition(
        metric_id="e2e_output_tokens_per_s",
        unit="tok/s",
        direction="higher_better",
        procedure_version="v1",
        aggregation="median",
        definition="completion tokens / full request wall time, tok/s, higher is better",
    ),
    "aggregate_output_tokens_per_s": MetricDefinition(
        metric_id="aggregate_output_tokens_per_s",
        unit="tok/s",
        direction="higher_better",
        procedure_version="v1",
        aggregation="group_wall",
        definition=(
            "sum completion tokens / common concurrent group wall interval, "
            "tok/s, higher is better"
        ),
    ),
    "ttft_s": MetricDefinition(
        metric_id="ttft_s",
        unit="s",
        direction="lower_better",
        procedure_version="v1",
        aggregation="median",
        definition="request start to first generated token, seconds, lower is better",
    ),
    "api_latency_s": MetricDefinition(
        metric_id="api_latency_s",
        unit="s",
        direction="lower_better",
        procedure_version="v1",
        aggregation="median",
        definition="full API-call wall time, seconds, lower is better",
    ),
}

class _FrozenMetricRegistry(Mapping[str, MetricDefinition]):
    """Immutable, read-only metric registry."""

    def __init__(self, definitions: Mapping[str, MetricDefinition]) -> None:
        self._definitions: dict[str, MetricDefinition] = dict(definitions)

    def __getitem__(self, key: str) -> MetricDefinition:
        return self._definitions[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._definitions)

    def __len__(self) -> int:
        return len(self._definitions)

    def __contains__(self, key: object) -> bool:
        return key in self._definitions


registry: _FrozenMetricRegistry = _FrozenMetricRegistry(_DEFINITIONS)


def comparable(a: MetricSample, b: MetricSample) -> bool:
    """True iff both samples share metric ID and all semantic dimensions."""
    return a.metric_id == b.metric_id and a.dimensions == b.dimensions
