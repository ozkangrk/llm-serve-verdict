# Adapter SDK and support matrix (v0.4)

Adapters normalize externally-produced benchmark JSON into explicit metric
semantics. They do not execute source content, infer absent fields, or make
incompatible metrics equivalent. All three initial adapters are
**experimental** because their upstream saved-result formats are not yet
published as stable versioned JSON schemas.

## Contract

Each adapter declares:

- unique `adapter_id`;
- source version(s) inspected;
- emitted canonical metric IDs;
- procedure/aggregation/direction/unit for every metric;
- limitations and compatibility status;
- strict `detect()` and fail-closed `parse()` behavior.

`AdapterRegistry.detect_one()` requires exactly one match. Zero matches produce
`UNSUPPORTED_SCHEMA`; multiple matches are an ambiguity error. Registry bindings,
normalized evidence, metric samples and semantic dimensions are immutable.

## Support matrix

| Adapter | Detection identity | Metrics mapped | Status / limitations |
|---|---|---|---|
| `vllm.serve` | `backend=vllm`, `endpoint_type=vllm`, no SGLang `server_info` | request throughput, output-token throughput, request success, p50/p95 TTFT when present | Experimental; upstream result has no stable schema version. Only present fields are emitted. |
| `sglang.serving` | `backend=sglang`, `server_info`, text/vision token fields and ITL | request/output throughput, p50/p95 TTFT, p95 E2E, p95 ITL | Experimental; no failed-request denominator in saved result, so success rate is not inferred. |
| `guidellm.report` | report `metadata.version=2` plus `guidellm_version` | request success/throughput, p50/p95 TTFT, p95 ITL, output-token throughput | Experimental; one benchmark entry per normalized evidence object; successful distributions only. Request latency is deferred because its source unit is seconds while the current canonical E2E metric is milliseconds. |

## Upstream sources inspected

The following immutable upstream revisions were inspected on 2026-08-19:

- vLLM commit [`d626108b1841888ec90aced33367149a6bbc7e4b`](https://github.com/vllm-project/vllm/blob/d626108b1841888ec90aced33367149a6bbc7e4b/vllm/benchmarks/serve.py), especially the saved result construction around lines 1259–1308 and backend identity around lines 2217–2218.
- SGLang commit [`32d98aad1339164cae522723b87af86ab11f7134`](https://github.com/sgl-project/sglang/blob/32d98aad1339164cae522723b87af86ab11f7134/python/sglang/benchmark/serving.py), saved result construction around lines 1782–1840.
- GuideLLM commit [`b3c4c420fb5a79ca7fcd0656668adad07cd8bcb2`](https://github.com/vllm-project/guidellm/tree/b3c4c420fb5a79ca7fcd0656668adad07cd8bcb2/src/guidellm/benchmark/schemas): `report.py` (`metadata.version=2`), `benchmark.py`, `metrics.py`, and `src/guidellm/schemas/statistics.py` (`DistributionSummary`, percentiles and status breakdowns).

The minimized test fixtures contain only documented fields used by the adapter;
they are not performance claims and are not presented as real benchmark runs.

## Canonical metric semantics

| Metric ID | Unit | Direction | Source semantics |
|---|---|---|---|
| `request_throughput_rps` | request/s | higher better | Saved run rate / successful GuideLLM request distribution mean |
| `output_tokens_per_s` | token/s | higher better | Output token throughput, never total-token throughput |
| `request_success_rate` | ratio | higher better | Explicit completed/(completed+failed) or GuideLLM request totals |
| `ttft_p50_ms` | ms | lower better | Median TTFT |
| `ttft_p95_ms` | ms | lower better | p95 TTFT |
| `e2e_latency_p95_ms` | ms | lower better | SGLang p95 E2E latency |
| `itl_p95_ms` | ms | lower better | p95 inter-token latency |

GuideLLM request latency is not emitted. A future adapter version may add it
only with an explicit canonical unit and compatibility rule.

The legacy global metric registry still stores one procedure version per metric.
External adapter samples therefore remain in the adapter SDK model with their
source-specific procedure dimension; they are not inserted into that legacy
registry until Metric Registry v2 supports explicit procedure compatibility
rules. This is fail-closed and prevents accidental cross-tool comparison.

## Adding an adapter

1. Add a strict detector using stable source identity fields.
2. Declare capabilities and known limitations.
3. Add a minimized fixture derived from an upstream documented schema.
4. Test supported, malformed, non-finite, unsupported-version, secret-field and
   ambiguity paths.
5. Never expose generated text or raw errors in normalized evidence.
6. Do not add a metric until its unit, direction, procedure and aggregation are
   explicit.
