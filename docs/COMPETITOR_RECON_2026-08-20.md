# Serving Verdict — Rakip / Öncelik Araştırması (2026-08-20)

Kapsam: vLLM Production Stack, llm-d, NVIDIA Dynamo + GenAI-Perf/AIPerf, KServe,
BentoML/BentoCloud, Ray Serve, SkyPilot, SGLang (+genai-bench), GuideLLM,
Prometheus/Grafana/OTel ve doğrudan "inference benchmark control-plane" ürünleri.
Yöntem: resmi README/docs metinlerinin doğrudan çekilmesi (raw.githubusercontent.com,
docs alan adları) + GitHub API repo meta verisi. Star sayıları tarihli anlık görüntüdür,
kalite kanıtı değildir. Kod değişikliği yapılmadı.

## 1. Bulunan ürünler — kanıt özetleri

### vLLM Production Stack
- [github.com/vllm-project/production-stack](https://github.com/vllm-project/production-stack)
  (2.521★, 2026-08-18 push), resmi docs: [docs.vllm.ai/projects/production-stack](https://docs.vllm.ai/projects/production-stack)
- Helm ile K8s-native referans yığın: vLLM motorları + request router (KV-cache/session
  aware) + Prometheus+Grafana gözlemlenebilirlik + LMCache KV offload.
- Docs ağacında ayrıca: Benchmarking, KEDA autoscaling, KubeRay pipeline parallelism,
  sleep/wakeup mode, distributed tracing bölümleri var.
- Yerel/Docker Compose yaşam döngüsü sunmaz; K8s varsayar.

### llm-d
- [github.com/llm-d/llm-d](https://github.com/llm-d/llm-d) (4.075★), docs: [llm-d.ai](https://www.llm-d.ai)
- CNCF Sandbox (Red Hat, Google Cloud, IBM, CoreWeave, NVIDIA kurucu). vLLM/SGLang
  üstü orkestrasyon: prefix-cache aware routing, katmanlı KV offload, P/D
  disaggregation, wide expert parallelism, batch API, SLO-aware autoscaling.
- "Well-lit paths": benchmark edilmiş recipe + Helm chart rehberleri.
- Prism (prism.llm-d.ai) "yeniden üretilebilir benchmark" portalı olarak linkleniyor;
  JS SPA, içeriği bu araştırma kapsamında teyit edilemedi — **doğrulanmamış detay**.
- Performans rakamları (3x throughput vb.) README/blog iddialarıdır, bağımsız ölçüm
  değildir.

### NVIDIA Dynamo + GenAI-Perf/AIPerf
- [github.com/ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo) (7.807★),
  docs: [docs.nvidia.com/dynamo](https://docs.nvidia.com/dynamo/)
- Veri merkezi ölçeğinde SGLang/TensorRT-LLM/vLLM üstü orkestrasyon: disaggregated
  serving, KV-aware routing, KVBM, ModelExpress (hızlı cold-start), Planner
  (SLA-driven autoscaler), Grove (gang scheduling), fault tolerance + request
  migration. K8s'ta sıfır-konfig DGDR (beta) ve recipe koleksiyonu.
- README'deki 7x/80% vb. sonuçlar blog/partner kaynaklı iddialar — **bağımsız kanıt yok**.
- Benchmarking: "Benchmarking Guide — deployment topolojilerini AIPerf ile karşılaştır"
  (resmi docs linki README'de).
- **GenAI-Perf**: [triton-inference-server/perf_analyzer/genai-perf](https://github.com/triton-inference-server/perf_analyzer/tree/main/genai-perf).
  README'de açık uyarı: **"GenAI-Perf is being phased out… use AIPerf instead."**
  → Stratejik olarak önemli: NVIDIA'ın aktif benchmark aracı AIPerf'e kaydı.
- **AIPerf**: [github.com/ai-dynamo/aiperf](https://github.com/ai-dynamo/aiperf)
  (557★, aktif). README doğrulananlar: multiprocess mimari (ZMQ ile 10 servis),
  3 UI modu (gerçek zamanlı TUI dashboard / simple / headless), concurrency/rate/
  trace-replay benchmark modları, goodput (SLO tabanlı), parameter sweep + adaptive
  search, DCGM GPU telemetrisi, Prometheus uyumlu server metrics, OTel/MLflow/W&B
  telemetri extras'ları, multi-run karşılaştırma plotları.
- AIPerf benchmark **dashboard + telemetri** sunar; promotion kararı/verdict yetkisi
  sunmaz (README'de böyle bir yetenek tanımı yok).

### KServe
- [github.com/kserve/kserve](https://github.com/kserve/kserve) (5.807★),
  docs: [kserve.github.io/website](https://kserve.github.io/website)
- CNCF incubating, K8s inference platformu. vLLM ve llm-d backend desteği,
  OpenAI-uyumlu protokol, request-based autoscaling + scale-to-zero, canary rollout,
  InferenceGraph, model caching, KV offload.
- Canary = K8s içinde trafik kaydırma mekanizmasıdır; kanıta dayalı, fail-closed
  promotion **kararı** vermez.

### BentoML / BentoCloud
- [github.com/bentoml/BentoML](https://github.com/bentoml/BentoML) (8.793★)
- Python kütüphanesi: model inference API'leri, otomatik Docker image üretimi,
  dynamic batching, multi-model pipeline; yerel geliştirme → Docker/BentoCloud'a
  deploy. README doğrulandı; BentoCloud'un yönetilen servis özellikleri
  (docs.bentoml.com Cloudflare arkasında kaldığı için) bu araştırmada **doğrulanmadı**.

### Ray Serve
- [docs.ray.io/en/latest/serve/index.html](https://docs.ray.io/en/latest/serve/index.html)
  (Ray 2.57.0). Framework-agnostik, ölçeklenebilir programlanabilir serving; LLM için
  streaming, dynamic batching, multi-node/multi-GPU; fractional GPU.
- K8s entegrasyonu: [ray-project/kuberay](https://github.com/ray-project/kuberay)
  (2.640★) — Ray cluster'larını K8s'te çalıştırma toolkit'i.
- Benchmark/verdict yeteneği sunmaz; llmperf arşivlenmiş durumda (önceki
  MARKET_RECON.md notu, doğrulanmamış güncel durumu).

### SkyPilot
- [github.com/skypilot-org/skypilot](https://github.com/skypilot-org/skypilot)
  (10.512★), docs: [docs.skypilot.co](https://docs.skypilot.co/)
- Multi-cloud AI compute platformu; iş/cluster/endpoint soyutlamaları.
- **SkyPilot Endpoints** (Haz 2026 blog): tek YAML ile motor + autoscaler + gateway +
  sertifika + metrics içeren tam serving yığını, çoklu K8s cluster üzerinde tek
  endpoint URL; inference/training GPU paylaşımı. [blog](https://skypilot.ai/blog/skypilot-endpoints)
- Benchmark/verdict yetkisi yok; cluster provisioning + deploy + dashboard odaklı.

### SGLang + genai-bench
- [github.com/sgl-project/sglang](https://github.com/sgl-project/sglang) (32.150★) —
  motor + bench/ alt klasörü (builtin benchmark betikleri).
- [github.com/sgl-project/genai-bench](https://github.com/sgl-project/genai-bench)
  (321★): token-level benchmark; CLI + **canlı UI dashboard** (ilerleme/log/gerçek
  zamanlı metrik), Excel rapor + multi-run karşılaştırma plotları.
- Tekrar: benchmark + görselleştirme; verdict yetkisi yok.

### GuideLLM
- [github.com/vllm-project/guidellm](https://github.com/vllm-project/guidellm)
  (1.527★, aktif), docs repo içi. SLO-aware benchmark platformu.
- README doğrulananlar: TTFT/ITL/e2e tam dağılımlar; sync/concurrent/rate/sweep
  profilleri; gerçek (HF) + sentetik multimodal veri; tool calling benchmark;
  Mooncake trace replay (OTEL/WEKA replay aktif geliştirme); JSON/CSV/HTML çıktı
  ("regression tracking" ifadesiyle); in-process vLLM backend.
- GuideLLM kendi karşılaştırma tablosunda inference-perf, genai-bench, llm-perf,
  ollama-benchmark, vllm/benchmarks'ı listeliyor.
- Verdict/promotion yetkisi yok; çıktıları "analiz + regression tracking" olarak
  konumluyor.

### inference-perf (ek, doğrudan rakip aday)
- [github.com/kubernetes-sigs/inference-perf](https://github.com/kubernetes-sigs/inference-perf)
  (225★, aktif): production-scale GenAI benchmark; goodput, OTel trace replay,
  10k+ QPS, saturation sweep. K8s ekosistemi benchmark standardı adayı.
- Yine: benchmark aracı, verdict yetkisi yok.

### Prometheus / Grafana / OpenTelemetry
- Prometheus (TSDB + /metrics scrape) ve Grafana (dashboard) standart bileşenler;
  vLLM production-stack ve AIPerf bunları doğrudan kullanıyor/uyumlu sunuyor
  (yukarıdaki kanıtlar). OTel: AIPerf OTel telemetri, GuideLLM OTEL trace replay,
  vLLM distributed tracing bölümleri — sektörün gözlemlenebilirlik standardı.
- Kaynaklar: [prometheus.io/docs](https://prometheus.io/docs/),
  [grafana.com/docs](https://grafana.com/docs/),
  [opentelemetry.io/docs](https://opentelemetry.io/docs/)

### Doğrudan "inference benchmark control-plane" taraması
- GitHub arama (2026-08-20): `inference benchmark control plane` vb. sorgular
  yalnızca 0–1★'lık deneme repo'ları döndürdü (nerve, k3-inference-platform,
  amperes-bench, AdaptiveRL-Orchestrator). Ciddi bir doğrudan rakip kontrol düzlemi
  bulunamadı.
- En yakın yapılar: AIPerf (benchmark+dashboard, NVIDIA), GuideLLM (SLO benchmark,
  vLLM projeleri), llm-d Prism (reproducible benchmark portalı, detay doğrulanamadı),
  Dynamo AIConfigurator (konfig **simülasyonu**, GPU harcamadan — ölçüm değil),
  MLflow (experiment tracking; serving verdict yetkisi yok), Katib (K8s hyperparameter
  tuning; training job'ları için, serving kararı değil).
- Önceki MARKET_RECON.md'deki küçük ölçekli benzerler (ArmTune Serve,
  llm-serving-benchmarks) hâlâ niş; durum değişiklik gösterebilir — **güncellenmedi**.

## 2. Katman bazlı özellik çakışma matrisi

| Katman | Çözenler (kanıtli) | Boşluk |
|---|---|---|
| Runtime lifecycle (başlat/durdur/yenik yapılandır) | vLLM production-stack, KServe, llm-d, Dynamo, SkyPilot Endpoints, Ray Serve/KubeRay, BentoML (K8s/docker) | Yerel, opt-in, fail-closed **canlı laboratuvar** yaşam döngüsü; üretim dokunulmazlığı varsayılan olarak garanti eden katman |
| Docker/K8s dağıtımı | Hepsinde (Helm chart, CRD, Knative, KubeRay, tek-YAML stack'ler) | Çözüldü. Tekrar kurmak gerekmez; allowlist'li şablon tüketimi yeterli |
| Benchmark / load generation | GuideLLM, AIPerf (GenAI-Perf yerine), inference-perf, genai-bench, vLLM bench scripts, MLPerf Inference | Yoğun çözüldü. Değer: semantik normalizasyon + karar yetkisi, yeni yük üreteci değil |
| Canlı metrikler | Prometheus+Grafana (production-stack, llm-d), AIPerf server metrics + DCGM GPU telemetrisi, OTel, AIPerf/genai-bench TUI'ları | Çözüldü; ancak "kanıt olarak mühürlenen" kısa süreli lab serisi değil |
| Deney karşılaştırma | AIPerf multi-run plot + MLflow/W&B export, GuideLLM CSV/HTML, MLflow, genai-bench Excel | Kısmi: sayısal karşılaştırma var; protokol uyumu + gate mantığı yok |
| Deterministik promotion kararı | **Bulunamadı** (KServe canary mekanizma, karar değil) | **Ana boşluk** |
| İmzalanmış provenance | cosign/Sigstore (OCI imza) + SLSA (build attestation) primitifleri var; serving verdict'ına bağlayan ürün **yok** | **İkincil boşluk** |
| Yerel UI | AIPerf TUI, genai-bench dashboard, Grafana — hepsi uzak/servis odaklı | Loopback-only, verdict-öncelikli, yerel-first ürün yok |

## 3. Zaten çözülmüş olanlar (tekrar kurma listesi)

1. Container/K8s ile runtime ömür döngüsü → şablon **tüket**, yeniden üretme.
2. Yük üretimi ve LLM metrikleri (TTFT/ITL/goodput, sweep, trace replay) →
   GuideLLM/AIPerf/inference-perf çıktılarını **adapter ile içe al**; kendi quick
   benchmark'ını koru (fail-closed protokol kimliği).
3. Prometheus/Grafana/OTel gözlemlenebilirlik → **adapter**, yeni TSDB backend'i kurma.
4. Çoklu motor desteği (vLLM/SGLang/TGI/llama.cpp) → motor tarafı zaten olgun;
   ürün motor rekabeti yapmaz (önceki MARKET_RECON sonucunu koru).

## 4. Defansif wedge (tek cümle)

> **Serving Verdict, ölçüm yapmayı değil ölçme sonucunu bağlamla mühürlemeyi
> satan, yerel-first, fail-closed promotion otoritesidir: tam imaj digest + bayrak +
> GPU + runtime parmak izine bağlı, imzalanmış PROMOTE/REJECT/INCONCLUSIVE kararı.**

Neden savunulabilir:
- Yukarıdaki hiçbir ürün "karar" katmanını işgal etmiyor; K8s platformları (KServe
  canary, llm-d, Dynamo) trafik kaydırma/scale yapar, kanıt kontrolü yapmaz.
- AIPerf/GuideLLM çıktısı "rapor"dur; rapor → karar adımdaki semantik uyum (hangi
  metrik hangi koşulla eşleşebilir), gate mantığı ve tamper-evidence bu ürünün
  çekirdeğidir ve v0.3'te zaten kodlanmış sözleşmelerle (fail-closed verdict,
  canonical digest, loopback) hazır.
- Rakip genişledikçe (AIPerf dashboard'ı büyüdükçe) karar yetkisi onların
  bağımsızlık sorunu olur; bağımsız local authority pozisyonu zayıflamaz.

## 5. Mimari sınırlar

```
┌─────────────────────────────────────────────────────────┐
│  Serving Verdict core (local, loopback, saf)            │
│  • verdict/policy engine (v0.3 fail-closed sözleşme)    │
│  • evidence store (sealed artifact + canonical digest)  │
│  • provenance signer (offline DSSE + Ed25519)           │
│  • local UI (verdict-first, read-only history)          │
└──────────────▲────────────────────────────▲─────────────┘
        (kanıt: sealed artifact)     (karşılaştırma/verdict)
┌──────────────┴───────────┐   ┌────────────┴──────────────┐
│ Lab Orchestrator (opt-in)│   │ Benchmark client (var)     │
│ • allowlist'li OCI       │   │ • v0.3 quick/standard      │
│   Compose şablonları     │   │   frozen profile          │
│ • throwaway lab network  │   │ • GuideLLM/AIPerf artifact │
│ • digest-pinned pull     │   │   import adapter (pasif)   │
│ • read-only /metrics     │   │   (yeni yük üreteci yok)   │
│   scrape (lab içi)       │   └────────────────────────────┘
│ • teardown + timeout     │
│ • Docker Engine API      │
│   (minimal adapter; exec │
│   YOK, privileged YOK)   │
└──────────────────────────┘
```

Sınır kuralları:
- **Core asla Docker'a bakmaz.** Lab Orchestrator ayrı modül; core yalnızca üretilen
  artifact'ları (canonical digest ile) kabul eder. Orchestrator devre dışıyken ürün
  v0.3 gibi çalışmaya devam eder.
- **Üretim dokunulmazlığı varsayılan:** orchestrator yalnızca kendi oluşturduğu
  `sv-lab-<runid>` adlı geçici network'e bağlanır; mevcut container/network/service
  listesi değiştirilmez, okunur. Üretim endpoint'leri yalnızca v0.3 endpoint sözleşmesi
  (loopback default, remote `--allow-remote`) ile gözlemlenir.
- **Şablon allowlist'i:** Compose YAML'ı kullanıcı yazamaz; ürün, imzalı/hasarlı-kanıt
  (tamamlayıcı olarak) bir şablon manifestosundaki kayıt üzerinden şablon seçer.
  Her kayıt: imaj adı, **sabit digest**, bayrak listesi (whitelist), model kaynağı,
  GPU gereksinimi, metrics portu, hazır-poz (readiness) endpoint'i.
- **Doğrulanmış spec yakalama (evidence):** imaj `@sha256:…` digest'i, tam CLI bayrak
  listesi, GPU modeli + sürüm (nvidia-smi/CUDA sürümü), container runtime sürümü
  (docker/podman version), compose sürümü, host parmak izi (OS kernel, CPU modeli),
  run başlangıç/bitiş zaman damgası, pull kaynağı (registry). Bunlar artifact'ın
  `lab-spec` alanına girer; eksik alan → `UNMEASURABLE`, uydurma değer yasak (v0.3
  ilkesi).
- **Read-only metrik:** lab container'larının `/metrics` (Prometheus text format)
  çıktısı yalnızca lab network içinden periyodik örneklenir; örnekler artifact'a
  gömülür. Yazma/yönetim endpoint'leri çağrılmaz.
- **Verdict girdisi:** sealed benchmark artifact + lab-spec + policy → deterministik
  karar (v0.3 motoru değişmez).

## 6. 2 aşamalı dikey MVP

### Aşama 1 — "Lab Run" (gözlem + kanıt)
1. Kullanıcı UI'da allowlist'li bir şablon seçer (v1 seti: vLLM-cuimage, SGLang,
   llama.cpp server, TGI — hepsi digest-pinned).
2. Orchestrator: geçici network `sv-lab-<runid>` → imaj pull (digest doğrulamalı;
   digest uyuşmazlığı = fail) → container başlat (fixed args; `--rm` eşdeğeri,
   resource limit, GPU passthrough yalnızca şablonda beyan edilen miktar kadar) →
   readiness bekleme (TCP değil, `/models`/chat preflight — v0.3 kuralı) →
   lab-spec yakalama → frozen quick benchmark çalıştır → `/metrics` örnekleri topla
   → **zorlu timeout'lu teardown** (container + network + lab volume'ları; başarısız
   teardown bile run'ı `FAILED` yapar, yarım-başarı üretmez).
3. Çıktı: sealed `lab-run` artifact (v1 schema) + mevcut v0.3 baseline'a karşı
   otomatik karşılaştırma + verdict.
- Üretim değişikliği: **yok** (varsayılan ve tasarım gereği).
- Tutarım kontrolü: v0.3 non-goals korunur (LLM judge yok, model-üretimi kod yok,
  remote multi-user yok, loopback-only UI).

### Aşama 2 — "Verdict + Provenance + Karşılaştırma"
1. **İmzalı verdict bundle:** kanıt hasarları + lab-spec + policy sürümü + karar,
   ed25519 imza; `verify` komutu (exit 4 = bütünlük hatası) v0.3 ile aynı.
2. **Experiment compare görünümü:** N lab-run yan yana; metrik deltaları, gate
   tablosu, protokol uyumsuzluğu → otomatik `INCONCLUSIVE` (v0.3 kuralı).
3. **Promotion gate raporu:** "bu karar şu tam koşullara bağlıdır" sayfası +
   inert (çalıştırılmayan) uygulama/rollback reçetesi — v0.3 rollback yaklaşımıyla
   tutarlı; otomatik uygulama **yok**.
4. Opsiyonel: GuideLLM/AIPerf JSON içe alma (pasif adapter; import edilen
   değerler `external` kanıt sınıfı olarak işaretlenir, ürünün kendi gate'lerinde
   birincil kanıt sayılmaz).

## 7. Güvenlik riskleri ve azaltımlar

1. **Docker socket ≈ root:** orchestrator'daki her bug host root yetkisi demektir.
   Azaltım: minimal Engine API yüzeyi (sadece create/run/pull/network; `exec` ve
   `volume mount --host` YASAK — model ağırlıkları için okunur, pinli bir yol);
   privileged/containerd socket yok; orchestrator yalnızca açık env flag + UI'da
   kullanıcı onayı ile etkinleşir; rootless Podman alternatifi belgelenir; test
   gereksinimi: "arbitrary subprocess input yok" invarianti (v0.3) burada da aynen
   korunur. **Kalıntı risk kabul edilir ve dokümante edilir** (local-first ürün
   varsayımı; CI'da opt-in).
2. **Secrets:** API key'leri yalnızca env'den (v0.3 kuralı); lab container'larına
   host env pas geçilmez; Compose şablonlarında secret alanı kullanıcı girdisi
   kabul etmez; artifact/log/UI'ya sızma testi (v0.3 test #10) lab-spec için de
   genişletilir.
3. **Remote endpoint'ler:** lab networkü varsayılan olarak izole (host servislere
   erişim yok); imaj/model çekimi = remote supply chain → allowlist'li registry
   (varsayılan: docker.io, ghcr.io, nvcr.io) + digest pin + (opsiyonel) cosign
   imza doğrulaması; model ağırlıkları pinli yerel yol veya digest'li registry'den.
   Üretim endpoint'leri için v0.3 `--allow-remote` sözleşmesi aynen korunur.
4. **Supply chain (imaj + ağırlık + runtime):** hepsi yerelde doğrulanana kadar
   "güvenilmeyen girdi" (önceki MARKET_RECON ilkesi). Digest mismatch = fail;
   şablon manifestosunun kendisi de sürüm + hash'lidir; runtime (docker) sürümü
   provenance'a girer ki aynı karar tekrarlanabilir/kanıtlanabilir olsun.
   Imajın içindeki kod ürünün attack surface'ine dahildir → allowlist dar ve resmi
   motor imajları ile sınırlı tutulur.
5. **Benchmark'in kendisi yük üreticisidir:** lab networkünde DDoS-yeni
   davranış riski düşük (izole net) ama CPU/GPU kaynak bütçesi zorlanır: profile
   bazlı timeout + toplam bütçe (v0.3) + teardown garantisi.

## 8. Kabul demosu (acceptance)

Tek makinede (Docker + GPU ya da CPU) script'le, sıfır elle müdahale:
1. Baseline: allowlist'li vLLM şablonu (imaj @digest A, bayrak seti X) → lab net →
   quick benchmark → sealed artifact.
2. Aday: aynı model, bayrak seti Y (veya SGLang @digest B) → ikinci lab run.
3. `bench compare` → deterministik PROMOTE veya REJECT; UI'da lab-spec (digest,
   bayrak, GPU, runtime) + gate tablosu + imza doğrulama.
4. **Tamper testi:** artifact'ta tek metrik değeri değiştir → `verify` exit 4,
   karar `INCONCLUSIVE`/geçersiz (fail-closed kanıt).
5. **Negatif test:** allowlist dışı imaj referansı reddedilir (çıkış 2), remote
   endpoint onaysız reddedilir.
6. **Tekrarlanabilirlik:** aynı girişler → aynı canonical hash'ler (v0.3 TDD kapısı
   #11), iki çalıştırmada aynı karar.

## 9. Prometheus mu, sınırlı in-process zaman serisi mi?

**Öneri: MVP'de sınırlı in-process zaman serisi (ring buffer + artifact'a gömülü
örnek dizisi); Prometheus'u lab'a gömme.**
Gerekçeler:
- Lab run'ları dakikalar sürer, tek host, retention gerekmez; Prometheus ek
  container + TSDB + scrape config yükü getirir ve 15s varsayılan scrape çözünürlüğü
  kısa run'larda kaba kalır.
- Güvenlik modeli loopback-only + minimal yüzeydir; ek bir servis yüzeyi genişletir.
- Kanıt gereksinimi "seri grafiği" değil **mühürlü örnek dizisidir**; in-process
  örnekler doğrudan artifact digest'inin altına girer (Prometheus'ta bu bağ
  zayıflar — TSDB dışarıda durur).
- **Ancak** format olarak Prometheus text exposition kullanılır: motorlar zaten
  `/metrics` verir; AIPerf'ın "Prometheus-compatible server metrics" yaklaşımı
  sektör standardını gösteriyor. Böylece ileride (Aşama 2+) "uzun süreli izleme"
  modu istenirse aynı örnekler bir Prometheus remote_write/OTel export adapter'ı
  ile dışarı verilebilir — geriye uyumlu, çekirdek değişmez.
- Sonuç: **scrape et (Prometheus formatı), saklama (in-process, sınırlı),
  görselleştir (yerel UI); Prometheus daemon'ını ürünün parçası yapma.**

## 10. Kanıt kalitesi ve doğrulanmamış iddialar

- **Doğrulandı (birincil metin, 2026-08-20):** tüm bölüm 1 README/docs özetleri;
  GenAI-Perf phase-out uyarısı; AIPerf özellik listesi (README); vLLM
  production-stack Helm/Grafana/router (README + docs sayfası); llm-d CNCF sandbox
  + well-lit paths (README); Dynamo yetenek matrisi + DGDR (README); KServe özellik
  listesi (README); GuideLLM karşılaştırma tablosu (README); SkyPilot Endpoints
  (resmi blog sayfası); Ray Serve dokümanı (docs.ray.io); genai-bench/inference-perf
  (README).
- **Doğrulanmadı / dikkat:** llm-d Prism'in içeriği (JS SPA, erişilemedi);
  BentoCloud'un yönetilen servis detayları (Cloudflare arkası); Dynamo/llm-d
  performans rakamları (blog/partner iddiası, bağımsız ölçüm yok); `llmperf`
  arşiv durumu güncellenmedi; önceki MARKET_RECON'daki niş rakipler
  (ArmTune, llm-serving-benchmarks) 2026-08-17 tarihli snapshot'ta sabit.
- Star/push tarihleri anlık görüntüdür; "aktif" ifadesi push tarihi kanıtına dayalıdır.

## 11. Stratejik sonuç

Bileşen katmanı (runtime, K8s, yük üretimi, metrik) 2026'da derinden çözülmüş ve
CNCF/NVIDIA ekosistemi tarafından standartlaştırılmış durumda. Boş katman
**karar + kanıt yetkisi**dir: hiçbir platform benchmark çıktısını "bu tam
konfigürasyon, bu GPU, bu imaj digest'i, bu policy ile PROMOTE/REJECT" olarak
mühürleyip imzalamıyor. Serving Verdict'in 2 aşamalı yolu (lab-run → signed
verdict) bu boşluğu, rekabetin güçlü olduğu katmanları yeniden kurmadan, allowlist'li
şablon ve fail-closed invariants'lerle doldurur.
