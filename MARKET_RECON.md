# Inference Engineering Studio — Market Reconnaissance

Date: 2026-08-17
Method: GitHub CLI (`gh api`, `gh search repos`, README extraction) against public repository metadata and primary READMEs. Star counts and activity are a dated snapshot, not quality proof.

## Runtime engines — do not compete here

| Repository | Stars | Last push | Scope |
|---|---:|---|---|
| [vllm-project/vllm](https://github.com/vllm-project/vllm) | 89,249 | 2026-08-17 | High-throughput GPU LLM serving |
| [sgl-project/sglang](https://github.com/sgl-project/sglang) | 31,949 | 2026-08-17 | LLM/VLM serving, radix cache, speculative paths |
| [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) | 124,289 | 2026-08-17 | Portable C/C++ local inference |
| [NVIDIA/TensorRT-LLM](https://github.com/NVIDIA/TensorRT-LLM) | 14,396 | 2026-08-17 | NVIDIA-optimized runtime/build stack |
| [InternLM/lmdeploy](https://github.com/InternLM/lmdeploy) | 8,011 | 2026-08-17 | Compression, deployment and serving |
| [ollama/ollama](https://github.com/ollama/ollama) | 178,740 | 2026-08-16 | Local model UX/runtime packaging |
| [huggingface/text-generation-inference](https://github.com/huggingface/text-generation-inference) | 10,886 | 2026-03-21 | Archived; historical HF serving runtime |

Conclusion: no new engine, generic OpenAI wrapper, or inference gateway.

## Deployment/control planes — integrate, do not rebuild

| Repository | Stars | Scope |
|---|---:|---|
| [ray-project/ray](https://github.com/ray-project/ray) | 43,536 | Distributed compute + Serve |
| [kserve/kserve](https://github.com/kserve/kserve) | 5,799 | Kubernetes inference platform |
| [llm-d/llm-d](https://github.com/llm-d/llm-d) | 4,042 | Accelerator-aware inference on Kubernetes |
| [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo) | 7,785 | Datacenter-scale distributed serving |
| [vllm-project/production-stack](https://github.com/vllm-project/production-stack) | 2,511 | Reference K8s vLLM production stack |
| [gpustack/gpustack](https://github.com/gpustack/gpustack) | 5,499 | GPU cluster/model-serving management |
| [mostlygeek/llama-swap](https://github.com/mostlygeek/llama-swap) | 5,388 | Reliable local model swapping |
| [jvr0x/lmswitch](https://github.com/jvr0x/lmswitch) | 6 | YAML-driven local llama.cpp/vLLM switching |
| [BerriAI/litellm](https://github.com/BerriAI/litellm) | 56,522 | Multi-provider gateway/routing/observability |

Conclusion: runtime lifecycle must be an adapter boundary. MVP should observe/import first; mutation only after an audited template and rollback contract.

## Benchmarking/load generation — use existing tools

| Repository | Stars | Scope |
|---|---:|---|
| [vllm-project/guidellm](https://github.com/vllm-project/guidellm) | 1,513 | Real/synthetic workloads, TTFT/ITL/distributions, sweep profiles, JSON/CSV/HTML |
| [mlcommons/inference](https://github.com/mlcommons/inference) | 1,616 | MLPerf inference reference suite |
| [ray-project/llmperf](https://github.com/ray-project/llmperf) | 1,129 | Archived benchmark library |
| [huggingface/optimum-benchmark](https://github.com/huggingface/optimum-benchmark) | 339 | Multi-backend model/hardware benchmark utility |
| [ninehills/llm-inference-benchmark](https://github.com/ninehills/llm-inference-benchmark) | 438 | General LLM inference benchmark |
| [jvr0x/dgx-spark-bench](https://github.com/jvr0x/dgx-spark-bench) | 4 | DGX Spark recipes + async harness + schema + interactive dashboard |

`dgx-spark-bench` already implements real Spark runs, unique request prefixes, concurrency sweeps, provenance-rich JSON, recipes and a GitHub Pages dashboard. A dashboard + benchmark recipes alone is not a wedge.

Conclusion: Studio should invoke/import GuideLLM, runtime-native benchmark commands and existing artifacts. Its value is semantic normalization and decision authority, not another load generator.

## Quantization/model optimization — consume evidence

| Repository | Stars | Scope |
|---|---:|---|
| [vllm-project/llm-compressor](https://github.com/vllm-project/llm-compressor) | 3,689 | Deployment-oriented compression for vLLM |
| [NVIDIA/Model-Optimizer](https://github.com/NVIDIA/Model-Optimizer) | 3,447 | Quantization, distillation, pruning, NAS, speculative models |
| [ModelCloud/GPTQModel](https://github.com/ModelCloud/GPTQModel) | 1,232 | Cross-hardware GPTQ and runtime integrations |
| [huggingface/optimum-quanto](https://github.com/huggingface/optimum-quanto) | 1,053 | PyTorch quantization backend |
| [casper-hansen/AutoAWQ](https://github.com/casper-hansen/AutoAWQ) | 2,349 | Archived AWQ implementation |

Conclusion: do not quantize/download models in MVP. Treat model/checkpoint revision, quant recipe and published quality as untrusted inputs until locally verified.

## Observability — not our product

| Repository | Stars | Scope |
|---|---:|---|
| [openlit/openlit](https://github.com/openlit/openlit) | 2,693 | OTel-native LLM/GPU observability, dashboard, evals, prompts, vault |
| [traceloop/openllmetry](https://github.com/traceloop/openllmetry) | 7,379 | OpenTelemetry instrumentation for LLM apps |
| [NVIDIA/dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter) | 1,833 | NVIDIA GPU Prometheus metrics |
| [mlflow/mlflow](https://github.com/mlflow/mlflow) | 27,541 | Experiment/eval/agent/model platform |

Conclusion: system health is a supporting view. Prefer import/adapter support for OTel/DCGM rather than building a new observability backend.

## Closest optimization products

### ArmTune Serve

[luxmikant/Arm-Tune](https://github.com/luxmikant/Arm-Tune), 1 star, active 2026-08-14.

- Detects Arm64 hardware/ISA/NUMA.
- Sweeps GGUF quantization, llama.cpp builds, threads and concurrency.
- Uses Arm Performix counters.
- Applies a quality gate.
- Returns deployment recommendation/report/command.
- Provides local Gradio dashboard.

This is very close to the original Studio pitch, but restricted to Arm CPU + llama.cpp. Our differentiation cannot merely be “the same for GPU.”

### LLM Serving Benchmarks self-optimizing loop

[swajayresources/llm-serving-benchmarks](https://github.com/swajayresources/llm-serving-benchmarks), 0 stars, active 2026-08-10.

- Served model proposes one flag from an eight-knob whitelist.
- Driver validates proposal, benchmarks twice, judges run two, appends crash-safe CSV.
- Stops after three rounds below 1% gain; grid fallback on parse failure.
- Includes raw A100/H100 campaign data and negative results.

This establishes that “LLM proposes serving flag and loop benchmarks it” is not unique.

## Surviving wedge

The unclaimed core is not dashboard, benchmark, runtime switch, observability, or LLM-generated flag search. It is:

> **A local, evidence-authoritative promotion controller that translates heterogeneous benchmark outputs into explicit metric semantics, replays representative real workloads against isolated candidates, detects synthetic-to-production reversals, and issues a fail-closed PROMOTE / REJECT / INCONCLUSIVE decision with a verified rollback.**

Required differentiators:

1. **Metric semantic registry** — decode tok/s, e2e output tok/s, aggregate throughput, TTFT, ITL and prefill cannot be compared unless definitions match.
2. **Evidence authority** — artifact hashes, runtime/model revisions, actual command, engagement logs and request success are required.
3. **Real-workload replay** — synthetic wins are challenged against redacted/frozen representative requests.
4. **Promotion gate** — correctness, tool calling, process stability, latency and concurrency gates are code, not an LLM opinion.
5. **Rollback proof** — candidate lifecycle is incomplete until the baseline is restored and re-health-checked.
6. **Negative-result product UX** — the SGLang synthetic win / production crash is a first-class case study, not hidden failure.
7. **Executable playbook** — each concept links to a real experiment and its claim boundary.

## MVP implication

The initial UI can show GPU/runtime/docs/artifacts, but the hero flow must be:

```text
Import baseline evidence
→ define one candidate delta
→ run frozen synthetic + representative workload
→ verify semantic comparability and correctness
→ PROMOTE / REJECT / INCONCLUSIVE
→ prove rollback
→ publish local case study
```

First end-to-end fixtures:

- Qwen3.8 MTP2 → DSpark k=7: PROMOTE.
- vLLM → SGLang EAGLE: synthetic win, production crash, REJECT + rollback.

Do not claim support for every runtime/model. MVP supports importing our two evidence schemas and observing one OpenAI-compatible endpoint. Adapter interfaces may be documented; only exercised adapters are labeled supported.
