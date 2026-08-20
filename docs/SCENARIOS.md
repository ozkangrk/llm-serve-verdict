# LLM ServeVerdict — concrete usage scenarios

LLM ServeVerdict answers one narrow question:

> **Should this exact LLM serving candidate replace this exact baseline under
> this workload and policy?**

The examples below are illustrative workflows, not published performance claims.

## Scenario 1 — Faster vLLM upgrade breaks tool calling

**Actor:** Alex, inference engineer
**Trigger:** They upgrade vLLM and change batching/KV-cache flags. A quick test
looks faster.

### Inputs

- baseline endpoint/artifact: current production-like vLLM configuration;
- candidate endpoint/artifact: upgraded vLLM configuration;
- frozen application workload including normal chat, JSON output and tool calls;
- policy requiring tool-call validity, request success and process stability.

### What LLM ServeVerdict does

1. Verifies that baseline and candidate evidence refer to comparable model,
   workload, protocol and metric procedures.
2. Runs/imports repeated measured trials, excluding warmup.
3. Compares TTFT, output throughput and latency.
4. Applies correctness/stability hard gates before performance.
5. Seals the exact evidence hashes, policy, claim boundary and result.

### Outcome

The candidate is faster, but one required tool-call gate fails.

```text
REJECT / HARD_GATE_FAILED
```

**Next action:** Do not deploy. Fix the tool-call regression and run a new
candidate. Speed cannot override application correctness.

---

## Scenario 2 — Quantization appears faster, but the result is noisy

**Actor:** Mert, performance engineer
**Trigger:** He compares the baseline precision with a new quantization format.
The candidate wins in some runs and loses in others.

### Inputs

- at least the policy's minimum baseline/candidate trial count;
- identical frozen prompts, token budgets, concurrency and streaming mode;
- a policy requiring a minimum relative throughput improvement;
- a deterministic statistical seed and confidence level.

### What LLM ServeVerdict does

1. Retains every trial; no implicit outlier removal.
2. Computes a deterministic bootstrap confidence interval for the relative
   effect in the correct metric direction.
3. Compares the interval with the required improvement threshold.

### Outcome

The interval crosses the threshold. The candidate may be better, but the
available evidence does not establish it.

```text
INCONCLUSIVE / STATISTICAL_UNCERTAINTY
```

**Next action:** Collect more representative trials or improve experimental
control. Do not promote from a small apparent average win.

---

## Scenario 3 — SGLang candidate clearly improves serving and passes every gate

**Actor:** Platform inference team
**Trigger:** The team evaluates a migration from its current serving
configuration to an SGLang candidate.

### Inputs

- strict SGLang/vLLM/GuideLLM saved-result adapters or the built-in bounded
  OpenAI-compatible benchmark;
- correctness, structured-output and stability evidence;
- repeated trials under matching semantic dimensions;
- trusted evidence manifest and signer policy.

### What LLM ServeVerdict does

1. Rejects unknown/ambiguous adapter formats and missing semantics.
2. Verifies comparable conditions and required evidence.
3. Applies hard gates, regression gates and statistical effect policy in fixed
   order.
4. Creates a v0.4 bundle and signs it with offline DSSE + Ed25519.
5. Runs `llm-serve-verdict gate ... --require PROMOTE --fail-inconclusive` in CI.

### Outcome

The performance effect is statistically sufficient, no required metric
regresses beyond policy, all hard gates pass and the signer is trusted.

```text
PROMOTE / ALL_REQUIRED_GATES_PASSED
```

**Next action:** The deployment pipeline may continue. The verdict authorizes
only the claim boundary recorded in the signed bundle; it is not a universal
claim about SGLang or the model.

---

## Scenario 4 — Someone edits a verdict or evidence value after the run

**Actor:** Reviewer or CI pipeline
**Trigger:** A stored verdict says PROMOTE, but an artifact value or reason code
was modified after issuance.

### What LLM ServeVerdict does

- recomputes the canonical digest;
- verifies the DSSE payload and Ed25519 signature when required;
- checks the signer/key against the local trust policy.

### Outcome

```text
verification failure / exit 4
```

This is not `REJECT` or `INCONCLUSIVE`: no valid verdict exists because the
artifact failed integrity/authenticity verification.

---

## What the product does not do today

v0.4 does not start, stop or mutate production LLM servers. It connects to an
operator-selected OpenAI-compatible endpoint or imports sealed benchmark
artifacts. The opt-in Docker Inference Lab is a separate v0.5 workstream with
its own explicit security boundary.
