"""Bounded, read-only Prometheus telemetry normalization for Inference Lab."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from threading import RLock

_METRIC_RE = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*$")
_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+"
    r"(?P<value>\S+)(?:\s+(?P<timestamp>\d+))?$"
)
_LABEL_RE = re.compile(r'(?P<key>[A-Za-z_][A-Za-z0-9_]*)="(?P<value>(?:\\.|[^"\\])*)"')
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.:+-]{0,128}$")


class TelemetryError(ValueError):
    """Telemetry input is malformed, unsafe, or outside a hard bound."""


@dataclass(frozen=True, slots=True)
class MetricBinding:
    source_name: str
    metric_id: str
    unit: str
    direction: str
    procedure_version: str
    allowed_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _METRIC_RE.fullmatch(self.source_name):
            raise TelemetryError("source metric name is invalid")
        if not isinstance(self.metric_id, str) or not self.metric_id:
            raise TelemetryError("canonical metric ID is invalid")
        if self.direction not in ("higher_better", "lower_better", "neutral"):
            raise TelemetryError("metric direction is invalid")
        if not isinstance(self.unit, str) or not self.unit:
            raise TelemetryError("metric unit is invalid")
        if not isinstance(self.procedure_version, str) or not self.procedure_version:
            raise TelemetryError("procedure version is invalid")
        labels = tuple(self.allowed_labels)
        if len(set(labels)) != len(labels) or not all(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label) for label in labels
        ):
            raise TelemetryError("allowed label names are invalid")
        object.__setattr__(self, "allowed_labels", labels)


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    offset_s: float
    metric_id: str
    source_name: str
    value: float
    unit: str
    direction: str
    procedure_version: str
    labels: tuple[tuple[str, str], ...]
    status: str = "ok"

    def __post_init__(self) -> None:
        object.__setattr__(self, "offset_s", _offset(self.offset_s))
        if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
            raise TelemetryError("telemetry sample value must be numeric")
        value = float(self.value)
        if not math.isfinite(value):
            raise TelemetryError("telemetry sample value must be finite")
        object.__setattr__(self, "value", value)
        if self.direction not in ("higher_better", "lower_better", "neutral"):
            raise TelemetryError("telemetry sample direction is invalid")
        if self.status != "ok":
            raise TelemetryError("telemetry sample status is invalid")
        if not _METRIC_RE.fullmatch(self.source_name):
            raise TelemetryError("telemetry sample source is invalid")
        for value_text in (self.metric_id, self.unit, self.procedure_version):
            if not isinstance(value_text, str) or not value_text:
                raise TelemetryError("telemetry sample metadata is invalid")
        labels = tuple(self.labels)
        if labels != tuple(sorted(labels)) or len(set(labels)) != len(labels):
            raise TelemetryError("telemetry sample labels are not canonical")
        for pair in labels:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not all(isinstance(item, str) for item in pair)
                or not _SAFE_LABEL_RE.fullmatch(pair[1])
            ):
                raise TelemetryError("telemetry sample labels are invalid")
        object.__setattr__(self, "labels", labels)


@dataclass(frozen=True, slots=True)
class TelemetryFailure:
    offset_s: float
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "offset_s", _offset(self.offset_s))
        if self.status not in ("timeout", "invalid", "unavailable"):
            raise TelemetryError("telemetry failure status is invalid")


def _offset(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TelemetryError("telemetry offset must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise TelemetryError("telemetry offset must be finite and non-negative")
    return result


def _parse_labels(raw: str | None, allowed: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    if raw is None or raw == "":
        return ()
    position = 0
    parsed: dict[str, str] = {}
    while position < len(raw):
        match = _LABEL_RE.match(raw, position)
        if match is None:
            raise TelemetryError("allowlisted metric labels are malformed")
        key = match.group("key")
        value = match.group("value").replace(r"\n", "\n").replace(r'\"', '"').replace(r"\\", "\\")
        if key in parsed:
            raise TelemetryError("allowlisted metric has duplicate labels")
        if key in allowed:
            lowered = value.lower()
            if (
                not _SAFE_LABEL_RE.fullmatch(value)
                or "sk-" in lowered
                or lowered.startswith("bearer")
                or "/" in value
                or "\\" in value
            ):
                raise TelemetryError("allowlisted label value is unsafe")
            parsed[key] = value
        position = match.end()
        if position == len(raw):
            break
        if raw[position] != ",":
            raise TelemetryError("allowlisted metric labels are malformed")
        position += 1
    return tuple(sorted(parsed.items()))


def parse_prometheus_snapshot(
    body: bytes,
    *,
    offset_s: float,
    bindings: tuple[MetricBinding, ...],
    max_response_bytes: int = 64 * 1024,
    max_series: int = 64,
) -> tuple[TelemetrySample, ...]:
    """Parse only declared metrics; raw input and unknown metrics are discarded."""
    offset = _offset(offset_s)
    if not isinstance(body, bytes):
        raise TelemetryError("telemetry response must be bytes")
    if len(body) > max_response_bytes:
        raise TelemetryError("telemetry response exceeds 64 KiB bound")
    if isinstance(max_series, bool) or not isinstance(max_series, int) or not 1 <= max_series <= 64:
        raise TelemetryError("max_series is out of bounds")
    by_source: dict[str, MetricBinding] = {}
    for declared in tuple(bindings):
        if declared.source_name in by_source:
            raise TelemetryError("duplicate binding source metric")
        by_source[declared.source_name] = declared
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TelemetryError("telemetry response is not UTF-8") from exc
    samples: list[TelemetrySample] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        source = line.split("{", 1)[0].split(None, 1)[0]
        binding = by_source.get(source)
        if binding is None:
            continue
        match = _LINE_RE.fullmatch(line)
        if match is None:
            raise TelemetryError("allowlisted metric line is malformed")
        try:
            value = float(match.group("value"))
        except ValueError as exc:
            raise TelemetryError("allowlisted metric value is invalid") from exc
        if not math.isfinite(value):
            raise TelemetryError("allowlisted metric value is not finite")
        labels = _parse_labels(match.group("labels"), binding.allowed_labels)
        series = (binding.metric_id, labels)
        if series in seen:
            raise TelemetryError("duplicate series in one scrape")
        seen.add(series)
        if len(seen) > max_series:
            raise TelemetryError("telemetry series count exceeds safety bound")
        samples.append(
            TelemetrySample(
                offset_s=offset,
                metric_id=binding.metric_id,
                source_name=binding.source_name,
                value=value,
                unit=binding.unit,
                direction=binding.direction,
                procedure_version=binding.procedure_version,
                labels=labels,
            )
        )
    return tuple(sorted(samples, key=lambda item: (item.metric_id, item.labels)))


class TelemetryBuffer:
    """Thread-safe, bounded telemetry and fixed-category scrape failures."""

    def __init__(self, *, max_samples: int, max_series: int) -> None:
        if isinstance(max_samples, bool) or not isinstance(max_samples, int) or not 1 <= max_samples <= 3600:
            raise TelemetryError("max_samples is out of bounds")
        if isinstance(max_series, bool) or not isinstance(max_series, int) or not 1 <= max_series <= 64:
            raise TelemetryError("max_series is out of bounds")
        self._max_samples = max_samples
        self._max_series = max_series
        self._samples: tuple[TelemetrySample, ...] = ()
        self._failures: tuple[TelemetryFailure, ...] = ()
        self._lock = RLock()

    def append(self, samples: tuple[TelemetrySample, ...]) -> None:
        if not isinstance(samples, tuple) or not all(isinstance(v, TelemetrySample) for v in samples):
            raise TelemetryError("append requires immutable telemetry samples")
        for sample in samples:
            TelemetrySample(
                offset_s=sample.offset_s,
                metric_id=sample.metric_id,
                source_name=sample.source_name,
                value=sample.value,
                unit=sample.unit,
                direction=sample.direction,
                procedure_version=sample.procedure_version,
                labels=sample.labels,
                status=sample.status,
            )
        with self._lock:
            candidate = (*self._samples, *samples)[-self._max_samples :]
            series = {(sample.metric_id, sample.labels) for sample in candidate}
            if len(series) > self._max_series:
                raise TelemetryError("telemetry buffer series limit exceeded")
            self._samples = candidate

    def record_failure(self, *, offset_s: float, status: str) -> None:
        offset = _offset(offset_s)
        if status not in ("timeout", "invalid", "unavailable"):
            raise TelemetryError("telemetry failure status is invalid")
        with self._lock:
            self._failures = (*self._failures, TelemetryFailure(offset, status))[-self._max_samples :]

    def snapshot(self) -> tuple[TelemetrySample, ...]:
        with self._lock:
            return tuple(self._samples)

    def failures(self) -> tuple[TelemetryFailure, ...]:
        with self._lock:
            return tuple(self._failures)
