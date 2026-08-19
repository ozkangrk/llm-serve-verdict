"""Config Advisor: deterministic, rule-based configuration analysis.

Given typed, normalized inputs (runtime family, benchmark facts, capacity
facts, current flags) the advisor:

- detects incompatible/unsafe flags and fails closed (``AdvisorError``);
- applies fixed deterministic rules to identify bottlenecks and emits ranked
  recommendations, each evidence-linked and carrying a reason, expected
  direction, risk, confidence boundary, a one-variable experiment plan and a
  rollback;
- builds a *safe, inert* dry-run launch/rollback recipe (argv arrays +
  shell-escaped text) for supported runtime profiles using only an
  allowlist of flags/types/ranges; nothing is ever executed;
- stamps the result with a canonical advisor artifact digest.

No LLM opinion participates: the output is a pure function of the input.
Recommendations never claim guaranteed gains.
"""
from __future__ import annotations

ADVISOR_SCHEMA_VERSION = "serving-verdict.advisor.v0.1"


class AdvisorError(Exception):
    """Fail-closed error raised on unsafe inputs, violations, or bad recipes."""

    exit_code: int = 2


from serving_verdict.advisor.advice import (  # noqa: E402
    AdvisorResult,
    ExperimentPlan,
    Rollback,
    advise,
)
from serving_verdict.advisor.flags import FlagSpec, FlagViolation  # noqa: E402
from serving_verdict.advisor.profiles import (  # noqa: E402
    LaunchCommand,
    ProfileSpec,
    RuntimeProfile,
    UnsupportedProfileError,
)
from serving_verdict.advisor.recipe import (  # noqa: E402
    Recipe,
    RollbackDiff,
    build_recipe,
    digest_of_artifact,
    verify_artifact,
)
from serving_verdict.advisor.schema import AdvisorInput, parse_advisor_input  # noqa: E402

__all__ = [
    "ADVISOR_SCHEMA_VERSION",
    "AdvisorError",
    "AdvisorInput",
    "AdvisorResult",
    "ExperimentPlan",
    "FlagSpec",
    "FlagViolation",
    "LaunchCommand",
    "ProfileSpec",
    "Recipe",
    "Rollback",
    "RollbackDiff",
    "RuntimeProfile",
    "UnsupportedProfileError",
    "advise",
    "build_recipe",
    "digest_of_artifact",
    "parse_advisor_input",
    "verify_artifact",
]
