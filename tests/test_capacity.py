"""TDD tests for exact KV-cache / weight memory formulas and capacity planning.

Golden vectors are hand-computed:

geometry (bf16 8B-class, GQA 32 heads / 8 KV heads):
  weights        = 8_000_000_000 params * 2 B = 16_000_000_000 B
  kv/token       = 2(K+V) * 32 layers * 8 kv_heads * 128 dim * 2 B = 131_072 B
  context 8192 + output 1024 = 9216 tokens
  kv/request     = 131_072 * 9216 = 1_207_959_552 B

RISK golden (24 GB, reserve 2 GB, target 3):
  usable_kv = 24e9 - 2e9 - 16e9 = 6e9 -> safe_max = floor(6e9 / 1207959552) = 4
  util = (16e9 + 2e9 + 3*1207959552) / 24e9 = 21623878656 / 24e9 = 0.900995 > 0.85

FIT golden (26 GB, reserve 2 GB, target 2):
  usable_kv = 8e9 -> safe_max = 6
  util = (18e9 + 2*1207959552) / 26e9 = 20415919104 / 26e9 = 0.785228 <= 0.85
"""
from __future__ import annotations

import math
from dataclasses import replace

import pytest

from serving_verdict.capacity import (
    CapacityInputError,
    CapacityPlan,
    ModelGeometry,
    kv_bytes_per_token,
    plan_capacity,
    weights_bytes,
)

GOLDEN = ModelGeometry(
    name="golden-8b-bf16",
    num_layers=32,
    num_attention_heads=32,
    num_kv_heads=8,
    head_dim=128,
    num_params=8_000_000_000,
    weight_bytes_per_param=2,
    kv_bytes_per_elem=2,
    max_context_tokens=32768,
)
MEM24 = 24_000_000_000
RES = 2_000_000_000


def test_weights_golden() -> None:
    assert weights_bytes(GOLDEN) == 16_000_000_000


def test_kv_per_token_golden() -> None:
    assert kv_bytes_per_token(GOLDEN) == 131_072


def test_mha_geometry_scales_kv() -> None:
    mha = replace(GOLDEN, num_kv_heads=32)
    assert kv_bytes_per_token(mha) == 4 * kv_bytes_per_token(GOLDEN)


def test_kv_per_request_golden() -> None:
    plan = plan_capacity(GOLDEN, MEM24, RES, 8192, 1024, 3)
    assert plan.kv_bytes_per_request == 1_207_959_552


def test_risk_golden() -> None:
    plan = plan_capacity(GOLDEN, MEM24, RES, 8192, 1024, 3)
    assert plan.classification == "RISK"
    assert plan.safe_max_concurrency == 4
    assert plan.usable_kv_bytes == 6_000_000_000
    assert plan.utilization == 0.900995
    assert plan.weights_bytes == 16_000_000_000


def test_fit_golden() -> None:
    plan = plan_capacity(GOLDEN, 26_000_000_000, RES, 8192, 1024, 2)
    assert plan.classification == "FIT"
    assert plan.safe_max_concurrency == 6
    assert plan.utilization == 0.785228


def test_no_fit_target_infeasible() -> None:
    plan = plan_capacity(GOLDEN, MEM24, RES, 8192, 1024, 5)
    assert plan.classification == "NO_FIT"
    assert plan.safe_max_concurrency == 4


def test_no_fit_weights_exceed_memory() -> None:
    plan = plan_capacity(GOLDEN, 17_000_000_000, RES, 8192, 1024, 1)
    assert plan.classification == "NO_FIT"
    assert plan.safe_max_concurrency == 0
    assert plan.usable_kv_bytes == 0
    assert math.isfinite(plan.utilization)


def test_reserve_zero() -> None:
    plan = plan_capacity(GOLDEN, 24_160_000_000, 0, 8192, 1024, 1)
    assert plan.safe_max_concurrency == 6


def test_plan_deterministic() -> None:
    a = plan_capacity(GOLDEN, MEM24, RES, 8192, 1024, 3)
    b = plan_capacity(GOLDEN, MEM24, RES, 8192, 1024, 3)
    assert a.to_dict() == b.to_dict()
    assert a == b


def test_assumptions_deterministic_and_exact() -> None:
    plan: CapacityPlan = plan_capacity(GOLDEN, MEM24, RES, 8192, 1024, 3)
    assert len(plan.assumptions) >= 5
    assert "16000000000" in plan.assumptions[0]
    assert "131072" in plan.assumptions[1]
    assert "worst case" in plan.assumptions[2]
    assert "2000000000" in plan.assumptions[3]
    assert plan.assumptions[4].startswith("No prefix-cache")


def test_uncertainty_notes_are_appended() -> None:
    plan = plan_capacity(
        GOLDEN, MEM24, RES, 8192, 1024, 3, uncertainty_notes=("host ram note",)
    )
    assert plan.assumptions[-1] == "host ram note"


def test_overflow_values_are_safe() -> None:
    plan = plan_capacity(GOLDEN, MEM24, RES, 10**15, 10**15, 1)
    assert plan.classification == "NO_FIT"
    assert plan.safe_max_concurrency == 0
    assert math.isfinite(plan.utilization)


BASE_GEOM = {
    "name": "g",
    "num_layers": 32,
    "num_attention_heads": 32,
    "num_kv_heads": 8,
    "head_dim": 128,
    "num_params": 100,
    "weight_bytes_per_param": 2,
    "kv_bytes_per_elem": 2,
    "max_context_tokens": 128,
}


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", ""),
        ("num_layers", 0),
        ("num_layers", -1),
        ("num_attention_heads", 0),
        ("num_kv_heads", 0),
        ("num_kv_heads", 33),  # exceeds num_attention_heads (32)
        ("head_dim", 0),
        ("num_params", 0),
        ("num_params", -5),
        ("weight_bytes_per_param", 0),
        ("kv_bytes_per_elem", -1),
        ("max_context_tokens", 0),
    ],
)
def test_invalid_geometry_rejected(field: str, value: object) -> None:
    kw = dict(BASE_GEOM)
    kw[field] = value
    with pytest.raises(CapacityInputError):
        ModelGeometry(**kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "total,reserve,ctx,out,target",
    [
        (0, RES, 8192, 1024, 1),
        (-5, RES, 8192, 1024, 1),
        (MEM24, -1, 8192, 1024, 1),
        (MEM24, RES, 0, 1024, 1),
        (MEM24, RES, -8192, 1024, 1),
        (MEM24, RES, 8192, 0, 1),
        (MEM24, RES, 8192, 1024, 0),
        (MEM24, RES, 8192, 1024, -1),
    ],
)
def test_invalid_plan_inputs(
    total: int, reserve: int, ctx: int, out: int, target: int
) -> None:
    with pytest.raises(CapacityInputError):
        plan_capacity(GOLDEN, total, reserve, ctx, out, target)


def test_non_integer_plan_inputs_rejected() -> None:
    with pytest.raises(CapacityInputError):
        plan_capacity(GOLDEN, 24.5, RES, 8192, 1024, 1)  # type: ignore[arg-type]
