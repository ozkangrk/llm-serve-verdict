"""Exact, typed memory formulas and safe-concurrency capacity planning.

The planner is deliberately *not* a neural estimator: every number is an
exact integer formula over the operator-supplied model geometry, and every
simplification is stated in ``assumptions`` so the artifact reads as a
proof, not an opinion. No LLM is involved at any point.

Formulas (all integers, no floats in the byte math):

- ``weights_bytes``      = ``num_params * weight_bytes_per_param``
- ``kv_bytes_per_token`` = ``2 * num_layers * num_kv_heads * head_dim
                            * kv_bytes_per_elem``
- ``kv_bytes_per_request`` = ``kv_bytes_per_token * (context_tokens +
                            max_output_tokens)``
- ``usable_kv_bytes``    = ``max(0, memory_total_bytes - weights_bytes -
                            reserve_bytes)``
- ``safe_max_concurrency`` = ``usable_kv_bytes // kv_bytes_per_request``

Classification (exact comparison, no heuristics):

- ``NO_FIT``: the target concurrency exceeds ``safe_max_concurrency``.
- ``RISK``:   the target fits but the resulting utilization (weights +
              reserve + target KV) exceeds 0.85 of the pool.
- ``FIT``:    the target fits and utilization stays within the bound.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Utilization above which a fitting target is classified RISK (not FIT).
MAX_SAFE_UTILIZATION = 0.85


class CapacityInputError(ValueError):
    """A capacity-plan input is invalid (negative, zero, non-integer, ...)."""


@dataclass(frozen=True, slots=True)
class ModelGeometry:
    """Typed model shape + precision input for exact memory math.

    Validation is fail-closed: zero/negative counts, non-integer values, or
    a KV head count above the attention head count raise
    :class:`CapacityInputError` at construction time.
    """

    name: str
    num_layers: int
    num_attention_heads: int
    num_kv_heads: int  # <= num_attention_heads (GQA/MQA allowed)
    head_dim: int
    num_params: int
    weight_bytes_per_param: int  # e.g. 2 for bf16, 1 for 8-bit int
    kv_bytes_per_elem: int  # e.g. 2 for bf16 KV, 1 for fp8 KV
    max_context_tokens: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise CapacityInputError("name must be a non-empty string")
        for label, value in (
            ("num_layers", self.num_layers),
            ("num_attention_heads", self.num_attention_heads),
            ("num_kv_heads", self.num_kv_heads),
            ("head_dim", self.head_dim),
            ("num_params", self.num_params),
            ("weight_bytes_per_param", self.weight_bytes_per_param),
            ("kv_bytes_per_elem", self.kv_bytes_per_elem),
            ("max_context_tokens", self.max_context_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise CapacityInputError(f"{label} must be a positive integer, got {value!r}")
        if self.num_kv_heads > self.num_attention_heads:
            raise CapacityInputError(
                "num_kv_heads must not exceed num_attention_heads "
                f"({self.num_kv_heads} > {self.num_attention_heads})"
            )


def weights_bytes(geometry: ModelGeometry) -> int:
    """Exact weight footprint in bytes."""
    return geometry.num_params * geometry.weight_bytes_per_param


def kv_bytes_per_token(geometry: ModelGeometry) -> int:
    """Exact per-token KV-cache footprint in bytes.

    ``2`` = K and V; GQA/MQA accounted for via ``num_kv_heads``.
    """
    return (
        2
        * geometry.num_layers
        * geometry.num_kv_heads
        * geometry.head_dim
        * geometry.kv_bytes_per_elem
    )


def _require_positive_int(label: str, value: object, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapacityInputError(f"{label} must be an integer, got {value!r}")
    if value < (0 if allow_zero else 1):
        raise CapacityInputError(
            f"{label} must be {'non-negative' if allow_zero else 'positive'}, got {value!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    """Exact capacity result for one model/device/context budget."""

    classification: str  # FIT | RISK | NO_FIT
    safe_max_concurrency: int
    target_concurrency: int
    usable_kv_bytes: int
    weights_bytes: int
    reserve_bytes: int
    kv_bytes_per_token: int
    kv_bytes_per_request: int
    utilization: float
    assumptions: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "safe_max_concurrency": self.safe_max_concurrency,
            "target_concurrency": self.target_concurrency,
            "usable_kv_bytes": self.usable_kv_bytes,
            "weights_bytes": self.weights_bytes,
            "reserve_bytes": self.reserve_bytes,
            "kv_bytes_per_token": self.kv_bytes_per_token,
            "kv_bytes_per_request": self.kv_bytes_per_request,
            "utilization": self.utilization,
            "assumptions": list(self.assumptions),
            "max_safe_utilization": MAX_SAFE_UTILIZATION,
        }


def plan_capacity(
    geometry: ModelGeometry,
    memory_total_bytes: int,
    reserve_bytes: int,
    context_tokens: int,
    max_output_tokens: int,
    target_concurrency: int,
    uncertainty_notes: tuple[str, ...] = (),
) -> CapacityPlan:
    """Compute the exact safe-max concurrency and classify FIT/RISK/NO_FIT.

    ``memory_total_bytes`` is the *assumed usable serving pool* for the
    chosen device (GPU VRAM for a discrete device, a chosen RAM allocation
    for unified memory). ``reserve_bytes`` is operator-configurable headroom
    for activations and fragmentation. ``uncertainty_notes`` (e.g. the
    unified-memory caveat from the hardware snapshot) are appended to the
    assumptions list verbatim.
    """
    memory_total = _require_positive_int("memory_total_bytes", memory_total_bytes)
    reserve = _require_positive_int("reserve_bytes", reserve_bytes, allow_zero=True)
    context = _require_positive_int("context_tokens", context_tokens)
    output = _require_positive_int("max_output_tokens", max_output_tokens)
    target = _require_positive_int("target_concurrency", target_concurrency)
    if reserve > memory_total:
        raise CapacityInputError("reserve_bytes must not exceed memory_total_bytes")

    w = weights_bytes(geometry)
    kv_tok = kv_bytes_per_token(geometry)
    tokens_per_request = context + output
    kv_request = kv_tok * tokens_per_request
    usable_kv = max(0, memory_total - w - reserve)
    safe_max = usable_kv // kv_request if kv_request > 0 else 0
    target_load = w + reserve + target * kv_request
    # memory_total > 0 guarantees a finite utilization even for huge inputs.
    utilization = round(target_load / memory_total, 6)

    if target > safe_max:
        classification = "NO_FIT"
    elif utilization > MAX_SAFE_UTILIZATION:
        classification = "RISK"
    else:
        classification = "FIT"

    assumptions = (
        f"weights = num_params * weight_bytes_per_param "
        f"= {geometry.num_params} * {geometry.weight_bytes_per_param} = {w} bytes",
        f"kv_per_token = 2 * {geometry.num_layers} layers * {geometry.num_kv_heads} "
        f"kv_heads * {geometry.head_dim} head_dim * {geometry.kv_bytes_per_elem} B "
        f"= {kv_tok} bytes/token",
        f"tokens_per_request = context {context} + output {output} = {tokens_per_request}; "
        f"KV grows to full length (no prefill/decode split, no eviction): worst case",
        f"reserve = {reserve} bytes held back for activations/fragmentation",
        "No prefix-cache sharing, speculative tokens, or offloading assumed "
        "(each request counts full worst-case KV)",
    ) + tuple(uncertainty_notes)

    return CapacityPlan(
        classification=classification,
        safe_max_concurrency=safe_max,
        target_concurrency=target,
        usable_kv_bytes=usable_kv,
        weights_bytes=w,
        reserve_bytes=reserve,
        kv_bytes_per_token=kv_tok,
        kv_bytes_per_request=kv_request,
        utilization=utilization,
        assumptions=assumptions,
    )
