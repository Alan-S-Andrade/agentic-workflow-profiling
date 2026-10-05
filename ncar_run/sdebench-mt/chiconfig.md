# Chameleon CHI@NCAR GH200 setup: SDEBench multi-tenancy with KV-cache migration

As of 2026-10-02. Companion to `ghconfig.md`, which describes the original runs on TACC Vista.
This file records how the same experiment is set up on a Chameleon CHI@NCAR GH200 bare-metal
node. The Vista setup is unchanged except for the items listed here. Paths are relative to
`~/sdebench-mt` (`$MT`, `/home/cc/sdebench-mt`) on the node unless absolute.

**What is being reproduced:** the `ghconfig.md` experiment at **N = 16 and N = 32** only, in two arms:

| Arm | vLLM KV cache | Purpose |
|---|---|---|
| `offload` | GPU (26.6 GiB, ~810K tokens) **plus 64 GiB CPU cache in Grace memory** (vLLM native offloading: blocks migrate HBM → CPU and are loaded back on a prefix hit) | the new condition ("KV-cache migration") |
| `baseline` | GPU only, identical to the Vista runs | same-machine control, separating the effect of migration from Vista vs Chameleon differences |
| `uva` (added 2026-10-03) | **8 GiB of weights in Grace memory, read in place over NVLink-C2C** (`--cpu-offload-gb 8`); the freed HBM goes to the GPU KV cache (34.7 GiB, ~1.05M tokens); no KV offload | the GH200 direct-access alternative: Grace memory used for weights to enlarge the HBM KV cache, compared against `baseline` |

Run structure: a **fresh vLLM server per (arm, N)**, **2N instance runs** per N (32 at N=16, 64 at N=32),
no dispatch deadline. The order is N=16 offload, N=16 baseline, N=32 offload, N=32 baseline.

Status on 2026-10-03: sweep `20261003-0506` (offload, baseline, uva × N=16/32) is complete; results in §8.2.

---

## 1. Chameleon resources

| Item | Value |
|---|---|
| Site / region | CHI@NCAR (`https://chi.hpc.ucar.edu:5000`), project `12b223bd6fb04c30a33bf45033f2f484` |
| Credentials | `/home/jc/chi-ncar/clouds-ncar.yaml` (restricted application credential `ncar_id`, roles member + reader, expires 2027-09-01) |
| Lease | `ghprobe` (`f0d1008e-c244-4430-ae4f-2f89d2ad36ce`), 1 × `gpu_gh200_96gb`, **ends 2026-10-06 01:00 UTC** |
| Instance | `gh200_dev_probe` (`ab7075bf-46cf-469e-9367-44ff646e09e7`), bare metal |
| Image | **`CC-Ubuntu24.04-CUDA-ARM64`** (`bdd2abf4-476d-45ed-bcd1-4192f192ebaf`). GH200 is aarch64: x86 images such as `CC-Ubuntu22.04-CUDA` go ACTIVE but never boot |
| Access | `ssh -i /home/jc/liveremote/keys/chi_ncar_gh200 -o UserKnownHostsFile=/home/jc/liveremote/keys/known_hosts cc@128.117.250.91` (keypair `chi-ncar-key`, floating IP from `public`, TCP 22 opened in the project's `default` security group). The key lives in the host registry `/home/jc/liveremote/` (`hosts.json` entry `gh200-dev-probe`); `/home/jc/chi-ncar/chi-ncar-key` is a symlink to it |
| Console | serial only (`openstack console url show --serial`); no VNC, no console log |
| CLI | `OS_CLIENT_CONFIG_FILE=/home/jc/chi-ncar/clouds-ncar.yaml OS_CLOUD=openstack openstack ...` (python-openstackclient + python-blazarclient) |

Nova cannot add a key to a running server. A new instance must be launched with `--key-name chi-ncar-key`;
otherwise the only fix is `openstack --os-compute-api-version 2.54 server rebuild --key-name`, which
wipes the disk.

## 2. Hardware and OS (differences from `ghconfig.md` §2)

| Item | Chameleon CHI@NCAR | Vista (`ghconfig.md`) |
|---|---|---|
| System | Quanta QuantaGrid S74G-2U, 1 × GH200 | TACC Vista `gh` / `gh-dev` |
| GPU | NVIDIA GH200 480GB (H100 96 GB HBM3), **driver 580.178.04, CUDA 13.0** | driver 590.48.01, CUDA 13.1 |
| CPU | 72 × Neoverse-V2, **4 KiB host pages** | 72 × Neoverse-V2, 64 KiB pages |
| Memory | NUMA 0: **~471 GiB LPDDR5X** (all cores); NUMA 1: 95 GiB HBM | NUMA 0: 120 GB |
| OS | Ubuntu 24.04.5, kernel 6.8.0-142-generic | (Vista RHEL) |
| Disks | 894 GB NVMe (`/`, holds everything); 1.7 TB SAS SSD `/dev/sda` unused | Lustre `$WORK` + 261 GB `/tmp` |
| `/dev/kvm` | `cc` has rw via ACL | world rw |
| `/dev/shm` | 236 GiB tmpfs (holds the 68.7 GB offload buffer) | |
| Scheduler | none (plain bash, run detached with `setsid nohup`) | Slurm |

CPU placement is unchanged: vLLM on cores 0–7, VMs on 8–71. Memory placement is unchanged: vLLM prefers
node 0 and VMs are bound to node 0. Admission control (`--min-mem-frac 0.10` of node 0) effectively
never triggers here, because node 0 is about 4× larger than on Vista.

## 3. Software versions (differences from `ghconfig.md` §3)

Everything in `ghconfig.md` §3 is pinned identically, with these exceptions and additions:

| Component | Here |
|---|---|
| Apptainer | **1.4.1 built from source** (`chameleon/install_apptainer.sh`, Go 1.27.1; the 1.4.1 release has no arm64 .deb and the PPA only has 1.5.4). Non-suid, with apt `squashfuse`/`fuse2fs` and an AppArmor `userns` profile for Ubuntu 24.04 |
| vLLM SIF | pulled **by digest** `vllm/vllm-openai@sha256:4864d466…` (arm64, CUDA 13.0.2). SIF sha256 `554520e5359809d0250ef5e716e6d190144bcd5840fe35af0a445b55331995c6`, 8,216,576,000 bytes. The file hash differs from Vista's `d78540a2…` because a SIF embeds its build time; the layers are identical |
| Dataset | `SWE-bench/SWE-bench_Verified` loaded **at revision `78f471bf…`** (the Vista script loaded the default branch) |
| Instance images | built **by digest** from `configs/image_digests.txt` (the `ghconfig.md` §7 digests) |
| Python | 3.12.15 (uv 0.12.22), all pins verified: mini-swe-agent 2.4.6, swebench 5.0.2, litellm 1.103.2, datasets 5.0.1, huggingface_hub 1.33.0, httpx 0.28.1, openai 2.54.0, PyYAML 6.0.3, numpy 2.5.3, matplotlib 3.11.2; unpinned pandas 3.0.6, aiohttp 3.14.3 |
| Cloud Hypervisor / kernel | v53.0 and `ch-release-v6.16.9-20260508`, sha256 verified against `ghconfig.md` §3; `ch-remote` sha256 `ade26617f74264467e1381f146fd1face6b8b0fb13c5ec84f4acedd72f972596` |
| Model | same weights/revision, stored once in the object store (§6) |

## 4. vLLM configuration

The launch command is `ghconfig.md` §4.1 unchanged (`slurm/serve_vllm.sh` via `slurm/common.sh`,
including the startup guard and the 780,000-token KV floor). `chameleon/env.sh` → `arm_args` appends
these flags through `EXTRA_VLLM_ARGS`:

```bash
# both arms: pin the GPU KV cache (KV_CACHE_BYTES; 0 = unpinned, as on Vista)
--kv-cache-memory-bytes 28668876800                         # 26.7 GiB ~ 812K tokens
# offload arm only
--kv-offloading-backend native --kv-offloading-size 64      # OFFLOAD_GIB
```

**Why pin the GPU KV cache.** On Vista, `--gpu-memory-utilization 0.85` alone gave 809,733–817,015
tokens. Here three unpinned starts gave 26.63 / 26.88 / **28.38 GiB** (809,733 / 817,015 / **863,618**
tokens): vLLM sizes the cache from a start-up memory profile, and that profile varies. A 6.6% swing is
comparable to the effect being measured at N=32, so both arms get exactly 26.7 GiB, the middle of the
Vista range. This is the only intentional difference from the Vista launch command apart from
offloading.

Effective configuration observed in the first (unpinned) smoke tests (`runs/chi-smoke-*`; the
pinned values are in §8):

| Setting | Offload arm | Baseline arm | Vista (`ghconfig.md` §10) |
|---|---|---|---|
| Version / Mamba mode / chunked prefill / attention | 0.30.0 / `align` / 8192 / `FLASH_ATTN` (FA3) | same | same |
| Weights on GPU | 50.22 GiB | 50.22 GiB | 50.22 GiB |
| Attention block size | 1568 tokens | 1568 | 1568 |
| Available KV cache memory | 26.63 GiB | 26.88 GiB | 26.6–26.84 GiB |
| GPU KV cache size | **809,733 tokens** | **817,015 tokens** | 809,733–817,015 |
| KV connector | `OffloadingConnector` + `CPUOffloadingSpec`, 68.70 GB mmap in `/dev/shm`, 16 CPU tensors | none | none |
| CPU cache capacity | ~1.98M tokens (stores ~34.7 KB per token) | — | — |
| `nvidia-smi` memory used | 80,340 MiB | 80,604 MiB | 79.4–82.5 GiB |
| Weight loading | 364 s | 503 s | |
| Time to `/health` | **780 s** | **850 s** | 150–165 s |

Startup is about 5× slower than on Vista, mainly because loading the safetensors takes 364 s here and
Python imports come from the squashfuse-mounted SIF. `serve_vllm.sh` therefore gained a
`HEALTH_TIMEOUT_S` knob (default 900, as before), and `chameleon/env.sh` sets it to 2400. Startup
happens before measurement, so it doesn't affect results.

**Offload metrics.** These are recorded at 1 Hz in `samples.jsonl` by the existing `metrics.py` regex:
`vllm:kv_offload_store_bytes_total`, `kv_offload_load_bytes_total`, `kv_offload_*_time`,
`kv_offload_cpu_cache_*_usage_perc`, `external_prefix_cache_{queries,hits}_total`.

**Reading the miss metric.** The per-call `usage.prompt_tokens_details.cached_tokens` counts local
(GPU) **and** external (CPU) prefix hits (`PrefillStats.num_cached_tokens`). So in the offload arm,
`analyze.py`'s "prefix-cache miss" means *recomputed*: neither in HBM nor in the CPU cache. The GPU/CPU
split is only available in aggregate, from the `external_prefix_cache_hits_total` and
`prefix_cache_hits_total` counters.

### 4.1 What "KV-cache migration" does here (vLLM 0.30.0 native offloading, read from the container's source)

Configuration selected by these flags: `OffloadingConnector` + `CPUOffloadingSpec` (not the `tiering`
package), **LRU** eviction, one CPU chunk = one 1568-token GPU block of one KV group.

| Question | Answer (vLLM 0.30.0 code) |
|---|---|
| Where does attention read offloaded KV? | Only from HBM. A CPU hit allocates GPU blocks and **copies** the KV into them first. Attention never reads host memory, even though the GH200 could (NVLink-C2C/ATS); nothing in the path is GH200-specific. |
| Copy mechanism | `swap_blocks_batch` → `cuMemcpyBatchAsync` (CUDA 13.0 build): DMA copy engines, one descriptor per (block, layer), on a pooled side stream. Host buffer: a 68.70 GB `MAP_SHARED` mmap in `/dev/shm` (shared by all TP/PP workers of the instance; unlinked after start-up), pre-faulted and pinned with `cudaHostRegister` (no failure warning in our logs, so pinned). |
| Store (GPU → CPU) | **Write-through, eagerly**: every step, every full 1568-token **prompt** block computed is queued for copy to CPU (submitted at the next step, on a side stream after the compute stream). Decode-generated blocks are never stored (`offload_prompt_only=True`, because reasoning is stripped from later turns anyway), and neither is the partial last block. Blocks already in the CPU cache are skipped. Storing doesn't wait for GPU eviction. |
| Linear-attention (GDN/mamba) state | Offloaded too, but only one state set per request, at the replay boundary `floor((P-1)/1568)*1568` (3 groups × 16 layers × 3.2 MB = 154 MB), plus shared-prefix junctions. |
| Lookup | GPU prefix cache first. The CPU is then searched from the GPU hit onwards: contiguous full blocks only, plus the GDN state exactly at the hit boundary, and at least one full block beyond the GPU hit. The hit is capped at `round_down(prompt-1, 1568)`. |
| Overlap | Loads are **asynchronous per request**: a request with a CPU hit goes to `WAITING_FOR_REMOTE_KVS` and **computes nothing** (not even its uncached suffix) until its whole load completes. Other requests keep running, and copies overlap their forward passes (separate streams). Loads are submitted after the current step's forward is launched, and the side stream waits for it, so a load starts after that step and the request is rescheduled 1–2 steps after the copy finishes. There is **no layer-wise pipelining**. A load and stores can be in flight at the same time; within one direction jobs are FIFO. |
| CPU cache full | LRU eviction of idle chunks. Chunks being loaded or stored are pinned, and if nothing is evictable the store is skipped (`kv_offload_allocation_failure`). Capacity is 1337 chunks × 51.4 MB = 68.70 GB, about 1.98M tokens at about 34.7 KB/token. |
| Byte check | A 48,608-token hit = 31 attention blocks × 51,380,224 B + 1 GDN state set 153,944,064 B = **1,746,731,008 B**, exactly the bytes measured by `offload_check.py`. |
| Metrics | `kv_offload_{load,store}_bytes/time` (time = GPU-event copy time), `external_prefix_cache_hits` (CPU-hit tokens). `kv_offload_cpu_cache_usage_perc` counts only chunks pinned by in-flight transfers, so it reads 0 when idle even if the cache is full. |

**Not GH200-specific.** This is vLLM's generic CUDA offloading path: its only platform check is
"CUDA-like", and the same code runs on an x86 + PCIe H100. vLLM mentions GH200 only elsewhere: weight
offloading, free-memory accounting on integrated GPUs, and NUMA detection. The integrated-GPU branch
doesn't apply here, because torch reports `is_integrated = 0` for this GH200. `nvidia-smi` shows ATS
addressing and C2C enabled, so the GPU *could* read Grace memory directly, but vLLM doesn't use that
for KV. What GH200 contributes is the link: the copies run over NVLink-C2C (133 / 66 GB/s measured)
instead of PCIe. The block size and per-request state come from the model; LRU, prompt-only stores,
store threshold 0 and one block per chunk are vLLM defaults, changeable through
`kv_connector_extra_config` (`eviction_policy`, `offload_prompt_only`, `store_threshold`,
`blocks_per_chunk`, `spec_name`; untested here).

Consequence for the measurements: a CPU hit restores exactly what a GPU hit would have (up to the
previous prompt's last full block). The partial last block (the "tail", < 1568 tokens) is recomputed
on every call in **both** arms, so offloading can only remove *eviction* recompute.

### 4.3 Weight-offload arm (`uva`) and LMCache (added 2026-10-03)

- **`uva`**: `--cpu-offload-gb 8` selects vLLM's `UVAOffloader`, which moves 8 GiB of parameters to
  pinned host memory and gives the GPU UVA views of them. Every forward pass reads those weights
  **directly from Grace memory** (zero-copy over NVLink-C2C, ~350 GB/s vs ~3.3 TB/s from HBM). That's
  roughly +25 ms per step, in exchange for +8 GiB of GPU KV (pin 37,258,811,392 B instead of
  28,668,876,800 B). `chameleon/env.sh`: `UVA_GIB=8`; `arm_args uva`.
- **LMCache `use_layerwise`: not run.** In vLLM 0.30.0 + LMCache 0.5.5, layer-wise loading exists only
  in `LMCacheConnectorV1`, which does not implement `SupportsHMA`. vLLM then disables the hybrid KV cache
  manager, and its own warning says hybrid SSM models "will fail at startup" without it; Qwen3.8 is such
  a model. LMCache's multi-process connector (`lmcache.integration.vllm.lmcache_mp_connector`) does
  support HMA and mamba `align` mode, but has no layer-wise loading. vLLM's other HMA-capable CPU
  connector (`SimpleCPUOffloadConnector`) loads whole requests too. Decision (user, 2026-10-03): skip
  LMCache.

### 4.4 KV and host-memory timeline (added 2026-10-03)

The 1 Hz sampler (`sdebench_mt/metrics.py`) additionally records:
- per NUMA node (`node.0` = Grace, `node.1` = GPU HBM): `MemFree`, `MemUsed`, `FilePages`, `AnonPages`, `Shmem`;
- the vLLM process tree (from `$VLLM_OUT/vllm.pid`): `vllm_mem_kb` = RssAnon / RssShmem / RssFile, total
  and per process. CUDA pinned host memory counts as RssShmem. That covers the CPU KV buffer (offload
  arm, 68.7 GB), the UVA-offloaded weights (uva arm: 12.8 GB shmem = 8.6 GB weights + pinned staging
  buffers) and other pinned buffers. Heaps are RssAnon.

The original fields are unchanged. `chameleon/timeline.py <run>` (run by `run_arm.sh`) writes
`N<n>/timeline.csv`, `N<n>/timeline.png` and `timeline_summary.json`, with panels for:
- GPU KV in use;
- CPU KV cache fill level (min of bytes stored and capacity);
- Grace memory by consumer (vLLM pinned memory, other vLLM memory, microVMs, everything else);
  "everything else" is mostly the agent tenant processes (about 0.5 GB each);
- running and waiting requests, plus live VMs.

Host memory is node 0 only: Linux counts GPU allocations as used memory on node 1, so system-wide
`MemTotal - MemAvailable` overstates host use by about 80 GB. Runs recorded before this change (the
offload/baseline sweep `20261003-0506`) show only the CPU KV buffer and microVMs.

### 4.2 Recompute measurement (added 2026-10-03)

`run_arm.sh` now also runs `chameleon/prefill_cost.py` (calibration, after the workload on the idle
server) and `chameleon/recompute.py` (analysis), writing `N<n>/prefill_cost.json`,
`N<n>/calls_recompute.csv` (one row per LLM call) and `recompute.md` / `recompute.json` per run.

Per call b that follows call a of the same conversation, with B = 1568:

| Quantity | Definition | Avoidable by offloading? |
|---|---|---|
| `cached` | `usage.prompt_tokens_details.cached_tokens` (GPU + CPU hits) | |
| `evict` | `max(0, floor(a.prompt/B)*B - cached)`: whole blocks of the previous prompt that were no longer cached | yes (if still in the CPU cache) |
| `tail` | `a.prompt - max(floor(a.prompt/B)*B, cached)`: the partial last block, recomputed on every call | no (partial blocks are never cached or stored) |
| `new` | the rest of the computed tokens: a's visible output + the new observation | no |
| calls with eviction | follow-up calls with `evict >= B` | |

GPU time: `prefill_cost.py` measures TTFT for cold prompts (2K–96K) and for incremental prompts on a cached
base, and fits `t = t0 + a*n + b*n*(c + n/2)`. On this node (pinned config): t0 = 96 ms,
a = 81.3 µs/token, b = 1.02 ps/token², fit error 5.9%, i.e. about 12.2K tokens/s at short context and
6.7K tokens/s for a 1568-token chunk at 64K context. Each recompute range [c, c+n) is charged
`a*n + b*n*(c + n/2)` GPU-seconds. Reported shares:
- of all prefill GPU time,
- of wall-clock GPU time (the run span),
- of LLM call time,
- per call (p50/p95).

GPU vs CPU hits, copy bytes, copy time and bandwidth come from the server counters in `samples.jsonl`.

Caveats:
- `evict` cannot tell eviction from a prefix that *changed* between calls (for example, a template
  rewriting an earlier turn). The no-pressure smoke runs bound this at 1/20 calls (N=1) and 0/152 (N=4).
- The time model charges recompute at the isolated-prefill rate. Under load, prefill shares steps with
  decode, so per-call latency effects also include queueing, which the model does not capture.
  The cross-arm comparison captures it.

## 5. Code changes relative to the Vista tree

The source came from a local copy of `$WORK/sdebench-mt` (a Claude scratchpad of the TACC session, dated
2026-10-01/02); `runs/` and the generated `configs/` files were not included. Permanent copies are in
`/home/jc/chi-ncar/sdebench-mt` (workstation), `~/sdebench-mt` (node) and the object store (`code/`).

| File | Change |
|---|---|
| `configs/instances_ok.txt`, `configs/image_digests.txt` | **recreated** from `ghconfig.md` §7 (19 instances, same order) |
| `configs/instances.jsonl`, `configs/gold_check.jsonl`, `configs/gold/` | regenerated here (19/19 gold patches resolved) |
| `vm/build_instance.sh` | pins `@digest` from `configs/image_digests.txt`; private `APPTAINER_CACHEDIR` per build (`ghconfig.md` §6/§11 describe this fix, but the copied script did not have it) |
| `slurm/serve_vllm.sh` | `HEALTH_TIMEOUT_S` knob (default 900 s, unchanged behaviour) |
| `chameleon/` (new) | see the table below |

| `chameleon/` file | Role |
|---|---|
| `env.sh` | `WORK=$HOME`, `MT=~/sdebench-mt`, PATH, `OFFLOAD_GIB=64`, `HEALTH_TIMEOUT_S=2400`, `KV_CACHE_BYTES=28668876800`, `arm_args <arm>` (the per-arm vLLM flags) |
| `bin/module` | no-op stand-in for TACC `module`, so `slurm/*.sh` run unchanged |
| `install_apptainer.sh` | Apptainer 1.4.1 from source + squashfuse + AppArmor profile |
| `setup_node.sh` | per-lease setup: apptainer, Python env, CH + kernel (sha256 checked), model link, KVM check |
| `build_images.sh` | port of `build_images.slurm`: SIF by digest, `instances.jsonl` at pinned revision, 19 images by digest, gold check (6 in parallel) |
| `store.sh` | object-store sync (`push`, `pull`, `push-runs`, `ls`) |
| `smoke.sh [offload\|baseline]` | port of `serve_smoke.slurm` + `offload_check.py`; `QUICK=1` skips N=1/N=4 |
| `prefill_cost.py`, `recompute.py` | prefill-cost calibration and per-call recompute analysis (§4.2), run by `run_arm.sh` |
| `synth_load.py`, `synth.sh <arm>` | synthetic multi-turn load used to validate §4.2 (not a benchmark) |
| `offload_check.py` | direct GPU→CPU→GPU migration test (§8) |
| `run_arm.sh <arm> <N> [jobs]` | one (arm, N) with a fresh server (port of `sweep_dev.slurm`), writes `arm.json` |
| `sweep.sh` | NS="16 32" × ARMS="offload baseline", then `store.sh push-runs` |

## 6. Object storage (persistent across leases)

The rclone remote `chi-ncar` uses the swift backend with the application credential; its config is
`/home/jc/chi-ncar/rclone.conf` (mode 0600, holds the secret), installed on the node as
`~/.config/rclone/rclone.conf`. No S3/EC2 keys are needed.

| Container / path | Content | Size |
|---|---|---|
| `llm-weights/Qwen/Qwen3.8-27B/` | BF16 weights at revision `1d4bf0f2…` + `REVISION` (33 objects) | 51.8 GiB |
| `sdebench-mt/code/` | repo incl. `chameleon/`, `configs/`, `ghconfig.md`, this file (no env, models, images, containers, runs) | 0.4 MiB |
| `sdebench-mt/vm/bin/`, `vm/kernel/` | cloud-hypervisor, ch-remote, guest `Image` | 29 MiB |
| `sdebench-mt/containers/` | `vllm-openai-v0.30.0.sif` | 7.65 GiB |
| `sdebench-mt/images/` | 19 × `<instance>.ext4.zst` (zstd -3 of the sparse 5.9–6.1 GB images; 116 GB apparent, 43 GB on disk) | 17.2 GiB |
| `sdebench-mt/runs/` | smoke tests now; sweep results after `store.sh push-runs` | |

Measured transfer rates node ↔ object store: about 100 MB/s up and 270 MB/s down (weights: 8.5 min
push, 3.4 min pull). The full benchmark-data push took 6 min 44 s, including compression.

## 7. Workload, agent, sandbox, measurement

Unchanged from `ghconfig.md` §5–§8: the same 19 instances in the same order, the same `mini.yaml`
overrides, sampling (T=0.6, top_p=0.95, max_tokens 16384), microVM shape (2 vCPU, 4096 MiB, 8 GiB
overlay), staggered closed-loop driver (5 s), and the same files per N. Each `N<n>/` additionally
holds `arm.json` (arm, offload args, host, driver).

## 8. Smoke test results (2026-10-02/03)

Final configuration (GPU KV pinned, `QUICK=1`; `runs/chi-smoke-offload_20261003-0017`,
`runs/chi-smoke-baseline_20261003-0034`):

| Check | Offload arm | Baseline arm |
|---|---|---|
| vLLM healthy / KV floor | 840 s, **811,190 tokens** ≥ 780,000 | 805 s, **811,190 tokens** |
| Chat with bash tool call | `{"command": "ls -la"}` | `{"command": "ls -la"}` |
| `offload_check.py`: 49,986-token prompt A, cold → GPU hit | 5.58 s → 0.29 s (48,608 cached) | 5.59 s → 0.30 s (48,608 cached) |
| … after a 1.31M-token flood (> GPU 811K, < CPU ~1.98M) | **migrated back from CPU**: 0.34 s, 48,608 cached, all external hits, 1.75 GB loaded, 47.1 GB stored | **recomputed**: 5.45 s, 0 cached, nothing stored or loaded |

Earlier full smoke test of the offload arm (unpinned KV, 809,733 tokens; `runs/chi-smoke-offload_20261002-2227`):
N=1 `django__django-10973` resolved (664 s, 21 calls); N=4 4/4 resolved, `max n_vmm 4 max running 4.0`;
no errors and no preemptions. The CPU store path was active the whole time: 91 GB stored by the end
of N=4. No loads occurred, because N ≤ 4 never fills the GPU cache.

### 8.1 Recompute measurement: validation (2026-10-03, not benchmark results)

**Real agent, no KV pressure** (smoke `chi-smoke-offload_20261002-2227`, N=4, 152 follow-up calls):
- Every follow-up call recomputed its previous prompt's partial last block (p50 737, p95 1,455 tokens).
- No call lost whole blocks.
- That tail is **56% of all prefill tokens**, but prefill is only 21 GPU-s in a 2,505 s run, so
  recompute is **0.46% of GPU time**. The workload is decode-bound: 190K completion tokens, 87% of
  them reasoning.

**Synthetic stress** (`synth.sh`, 32 tenants × 15 min, contexts up to 60K tokens, GPU KV 100% full in both arms):

| | baseline | offload |
|---|---|---|
| LLM calls completed | 479 | **856** |
| Follow-up calls with ≥1 evicted block | **71%** | 5% |
| Recomputed tokens per call, p50 / p95 | 16,335 / 30,269 | 792 / 1,567 |
| Recompute share of prefill tokens | 87% | 55% (mostly tail) |
| Recompute GPU time / share of wall-clock GPU time | 620 s / **66%** | 230 s / **25%** |
| … of which eviction | 587 s (62%) | 160 s (17%) |
| GPU hits / CPU hits | 1.47M / 0 | 1.29M / **20.0M** tokens |
| CPU → GPU copies | — | 778 GB at **133 GB/s** (5.9 s of copy time); stores 195 GB at 66 GB/s |
| GPU time the CPU hits avoided (estimate) | — | ~1,990 GPU-s |
| LLM call p50 / p95 | 59 s / 108 s | 26 s / 56 s |
| Generation throughput | 152 tok/s | 277 tok/s |
| Preemptions | 17 | 14 |

The calibration was identical in both arms (a = 81.0/81.3 µs/token, b = 1.02 ps/token², fit error 5–6%).
The synthetic load is far heavier than the Vista N=32 agent load (peak KV 68–84%, 5–15% of calls with
misses), so expect much smaller effects in the sweep.

### 8.2 Sweep `20261003-0506` results (2026-10-03, one run per cell)

Three arms × N = 16, 32 with 2N instance runs and a fresh server per (arm, N): offload and baseline
05:06–11:10 UTC, then uva 11:10–15:38 UTC (`phase2.sh`). There were no errors and no preemptions.
`runs/compare_20261003-0506.md` (written by `chameleon/compare.py`):

| arm | N | resolved | errors | makespan min | gen tok/s | LLM p50 s | LLM p95 s | peak GPU KV % | preempt | calls w/ evicted blocks | recompute / prefill tok | recompute GPU-s | evict share GPU time | tail share GPU time | GPU hits M tok | CPU hits M tok | CPU→GPU GB | peak CPU KV fill GB | peak VM RSS GB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 16 | 25/32 | 0 | 72 | 320 | 7.4 | 174 | 49 | 0 | 0.8% | 62% | 106 | 0.33% | 2.13% | 17.9 | 0.00 | 0.0 | – | 7.9 |
| baseline | 32 | 50/64 | 0 | 82 | 431 | 10.3 | 183 | 90 | 0 | 19.1% | 86% | 698 | 10.85% | 3.43% | 24.9 | 0.00 | 0.0 | – | 13.6 |
| offload | 16 | 25/32 | 0 | 65 | 330 | 7.4 | 157 | 41 | 0 | 0.1% | 59% | 87 | 0.04% | 2.21% | 15.2 | 0.07 | 3.5 | 68.7 | 7.0 |
| offload | 32 | 50/64 | 0 | 86 | 467 | 8.9 | 211 | 91 | 0 | 0.1% | 59% | 167 | 0.09% | 3.15% | 23.2 | 6.50 | 284.9 | 68.7 | 14.1 |
| uva | 16 | 26/32 | 0 | 100 | 181 | 13.1 | 280 | 34 | 0 | 0.2% | 57% | 210 | 0.07% | 3.43% | 16.1 | 0.00 | 0.0 | – | 6.9 |
| uva | 32 | 47/64 | 0 | 113 | 281 | 17.2 | 347 | 66 | 0 | 6.3% | 73% | 646 | 4.70% | 4.81% | 21.1 | 0.00 | 0.0 | – | 14.2 |

Findings:
- **N=16: no KV pressure.** Live-context demand peaked at 35–41% of GPU KV capacity, and every arm
  lost whole blocks on ≤ 0.8% of calls. Offload vs baseline differences there are run-to-run noise.
- **N=32: offloading removes eviction recompute.**
  - Without offloading, 19.1% of calls lost blocks, and recomputing them took 10.9% of GPU time.
  - With 64 GiB offload, 0.1% of calls lost blocks (6.5M tokens and 285 GB came back from Grace
    memory) and eviction took 0.09% of GPU time.
  - Generation throughput rose 431 → 467 tok/s (+8%) and median LLM latency fell 10.3 → 8.9 s (−14%).
    p95 latency and makespan (dominated by a few long instances) didn't improve, and resolved counts
    were equal (50/64).
- **Tail recompute** (the partial last block, recomputed every call) costs 2–5% of GPU time in every
  arm, and offloading can't remove it.
- **`uva` (8 GiB weights over C2C, +30% GPU KV) is a net loss.**
  - Decoding is about 2.4× slower (62.8 vs 25.9 ms between tokens at about 8 running), and a cold
    50K-token prefill takes 11.8 vs 5.6 s.
  - Throughput fell to 181 / 281 tok/s at N=16 / 32, median latency rose to 13.1 / 17.2 s, makespan
    rose to 100 / 113 min, and resolved fell to 47/64 at N=32 (more instances hit the 1-hour agent limit).
  - The bigger cache did cut lost-block calls at N=32 from 19.1% to 6.3%.
- **Eviction starts below nominal capacity.** At N=32 the sum of live conversation contexts peaked at
  69% (baseline) / 76% (offload) / 53% (uva) of the GPU KV token capacity, yet blocks were evicted.
  `kv_cache_usage_perc` counts only blocks held by running requests; conversations waiting on a tool
  hold theirs only as evictable cache. Per-request linear-attention state blocks and block-granular
  allocation probably also take pool space the token figure doesn't count (not verified).
- **Memory** (timelines in `N<n>/timeline.png`):
  - Grace memory used by vLLM: 68.7 GB pinned CPU KV buffer (offload; it fills completely even at
    N=16, because stores are write-through), 13.1 GB pinned (uva), none (baseline).
  - MicroVM RSS peaked at 7–8 GB at N=16 and 14 GB at N=32 (about 0.44 GB per VM).
  - HBM NUMA node: about 86–87 GB used (uva, new sampler).

Local copy of the summaries, CSVs and charts: `/home/jc/chi-ncar/sweep-results/20261003-0506/`.
Full runs: `chi-ncar:sdebench-mt/runs/chi-{offload,baseline,uva}_20261003-0506/`.

## 9. Procedures

### 9.1 New lease (from nothing)

```bash
# workstation, in /home/jc/chi-ncar (CLI venv: pip install python-openstackclient python-blazarclient)
export OS_CLIENT_CONFIG_FILE=$PWD/clouds-ncar.yaml OS_CLOUD=openstack
openstack reservation lease create --reservation min=1,max=1,resource_type=physical:host,resource_properties='["==","$node_type","gpu_gh200_96gb"]' \
  --start-date "YYYY-MM-DD HH:MM" --end-date "YYYY-MM-DD HH:MM" <lease-name>
RES=$(openstack reservation lease show <lease-name> -f value -c reservations | grep -oE '"id": "[^"]+"' | head -1 | cut -d'"' -f4)
openstack server create --image CC-Ubuntu24.04-CUDA-ARM64 --flavor baremetal --key-name chi-ncar-key \
  --network sharednet1 --hint reservation=$RES <server-name>          # ~15 min to ACTIVE + boot
openstack floating ip create public; openstack server add floating ip <server-name> <ip>
# node: weights, environment, benchmark data
scp -i chi-ncar-key rclone.conf weights.sh cc@<ip>:~
ssh -i chi-ncar-key cc@<ip> 'install -Dm600 ~/rclone.conf ~/.config/rclone/rclone.conf && rm ~/rclone.conf && install -D ~/weights.sh ~/bin/weights.sh'
rsync -a -e "ssh -i chi-ncar-key" sdebench-mt/ cc@<ip>:sdebench-mt/        # or on the node: rclone copy chi-ncar:sdebench-mt/code ~/sdebench-mt
ssh -i chi-ncar-key cc@<ip>
  ~/bin/weights.sh pull                         # 52 GB in ~3.5 min -> ~/models/Qwen3.8-27B
  bash ~/sdebench-mt/chameleon/setup_node.sh    # ~15 min (Apptainer build)
  bash ~/sdebench-mt/chameleon/store.sh pull    # SIF + 19 images (decompressed sparse)
```
To rebuild from scratch instead of `store.sh pull`, run `chameleon/build_images.sh` (about 25 minutes:
images ~2 min per batch of 6, SIF pull, gold check).

### 9.2 Smoke test (~40 min with N=1/N=4; ~20 min with `QUICK=1`)

```bash
cd ~/sdebench-mt && bash chameleon/smoke.sh offload     # QUICK=1 bash chameleon/smoke.sh baseline
```
Expect the §8 values: KV ≥ 780,000 tokens, a bash tool call, N=1 resolved, `max n_vmm 4 max running
4.0`, and for the offload arm an `OFFLOAD_CHECK … MIGRATED BACK FROM CPU` line.

### 9.3 The sweep (both arms, N = 16 and 32)

Nothing else may use the GPU: stop any other vLLM first, since `common.sh`'s cleanup also kills
`vllm serve` processes it did not start.

```bash
cd ~/sdebench-mt && setsid nohup bash chameleon/sweep.sh > ~/sweep.log 2>&1 < /dev/null &
# options: NS="16 32"  ARMS="offload baseline"  (also: uva)  OFFLOAD_GIB=64  UVA_GIB=8  SWEEP_ID=<id>
# a completed (arm, N) is skipped. chameleon/phase2.sh chains the uva arm after a running sweep.
tail -f ~/sweep.log
```
Output: `runs/chi-offload_<SWEEP_ID>/N16|N32` and `runs/chi-baseline_<SWEEP_ID>/N16|N32`, each with
`summary.md` from `analyze.py`, pushed to `chi-ncar:sdebench-mt/runs/` at the end (or run
`chameleon/store.sh push-runs`).

Estimated duration: about 15 min of vLLM startup per (arm, N), plus the runs themselves. The Vista
single-server sweep needed about 6 h for N = 1…32 at 2N jobs, and N=32 with 64 runs was the largest
share. Budget **6–8 h for all four**, so start it at least 10 h before the lease ends.

## 10. Verification checklist for the sweep

Per (arm, N), in `runs/chi-<arm>_<id>/N<n>/vllm.log` and `~/sweep.log`:

- the `ghconfig.md` §10 lines (version 0.30.0, `align`, 8192, `FLASH_ATTN`, 50.22 GiB, 1568,
  `KV cache tokens: … (minimum 780000), attempt 1`), with `vLLM healthy after` about 780–850 s here and
  the GPU KV cache **identical in every (arm, N)** because of the pin (§8 has the value);
- offload arm only: `Creating v1 connector with name: OffloadingConnector` and the `/dev/shm` mmap
  (68.70 GB);
- `samples.jsonl`: at N=32, `kv_offload_load_bytes_total` and `external_prefix_cache_hits_total` should
  rise (blocks coming back from the CPU) once GPU KV use approaches 100%; Vista N=32 peaked at 68–84%.
  `num_preemptions_total` should stay 0;
- `arm.json` matches the arm; `summary.json` `errors: 0`.

Reference values to compare against (Vista, `ghconfig.md` §1), split · single-server:
N=16: 24/32 · 25/32 resolved, 337 · 289 tok/s, p50 6.7 · 6.1 s, 48% · 44% peak KV, 1.7% · 0.9% misses;
N=32: 25/32 · 53/64, 319 · 506 tok/s, p50 7.1 · 9.4 s, 68% · 84% peak KV, 5.1% · 14.7% misses.
The N=32 single-server row is the closer comparison, since it also had 64 runs.

## 11. Pitfalls found on Chameleon

| Symptom | Cause | Fix |
|---|---|---|
| Instance ACTIVE but no ping/SSH, silent serial console | x86 image on an aarch64 GH200 node | use a `*-ARM64` image |
| No way into the instance | launched without `--key-name` | rebuild with `--key-name` (wipes disk) or relaunch |
| Apptainer 1.4.1 not installable | no arm64 .deb, PPA has only 1.5.x | `chameleon/install_apptainer.sh` (source build) |
| Apptainer: user namespace denied | Ubuntu 24.04 AppArmor userns restriction | AppArmor profile (in the installer) |
| `serve_vllm.sh` aborted under `set -e` | TACC `module` missing | `chameleon/bin/module` no-op |
| vLLM "not healthy after 900s" risk | 13–14 min startup here | `HEALTH_TIMEOUT_S=2400` |
| pip vLLM 0.30.0 (outside the container) fails at warm-up with `ninja`/nvcc header errors | FlashInfer JIT on the host: nvcc 12.6 / 13.4 vs headers | use the container (as here); for the pip build set `VLLM_USE_FLASHINFER_SAMPLER=0` |
| Remote `nohup … &` over SSH hangs the SSH client | background job keeps the session fds | `setsid nohup … < /dev/null & disown` |
| `pgrep -f`/`pkill -f <pattern>` matches its own shell | the pattern is in the command line | match on a PID, or on the full executable path |
| Offload check showed no CPU hit | flood larger than the CPU cache too (random words are ~3.1 tokens) | flood between GPU (~0.81M) and CPU (~1.98M) capacity |
| GPU KV capacity differs between starts (809K–864K tokens) | vLLM profiles activation / non-torch memory at each start | `--kv-cache-memory-bytes` pin (`KV_CACHE_BYTES`) |
