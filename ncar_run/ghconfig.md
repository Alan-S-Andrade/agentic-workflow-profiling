# GH200 configuration and reproduction guide: SDEBench multi-tenancy runs

As of 2026-10-02. This document is self-contained: it pins every version and setting used for the
two sweeps in `runs/` and gives the commands to reproduce them on TACC Vista. All paths are relative
to the project root `$WORK/sdebench-mt` (`/work/11863/jc9/vista/sdebench-mt`) unless absolute.

**What is being reproduced:** N = 1, 2, 4, 8, 16, 32 concurrent SWE-bench Verified ("SDEBench") agent
tenants on one GH200 node. All tenants share one Qwen3.8-27B vLLM server; every instance run executes
its tool commands in its own Cloud Hypervisor microVM.

---

## 1. Results to reproduce

Two independent sweeps, both 2026-10-02:

| Run | Directory | Slurm | Instance runs at N = 1/2/4/8/16/32 | vLLM server |
|---|---|---|---|---|
| Single-server sweep | `runs/sdebench-sweep_1041754/` | `gh`, job 1041754, 5 h 59 min | 8/8/8/16/32/64 | one server for all N |
| Split sweep | `runs/sdebench-sweep-dev_20261002-0109/` | `gh-dev`, one job per N | 6/8/8/16/32/32 | fresh server per N |

Reference values (split · single-server). Use them to check a reproduction; see §10 for tolerances.

| N | Resolved | Generation tok/s | LLM call p50 (s) | LLM call p95 (s) | Peak KV use | Calls with prefix-cache miss |
|---|---|---|---|---|---|---|
| 1 | 6/6 · 8/8 | 53 · 54 | 4.8 · 4.2 | 155 · 76 | 7% · 6% | 0% · 0% |
| 2 | 7/8 · 6/8 | 99 · 95 | 4.3 · 4.5 | 94 · 81 | 10% · 9% | 0% · 0% |
| 4 | 7/8 · 8/8 | 143 · 145 | 4.1 · 4.1 | 62 · 120 | 12% · 17% | 0.4% · 0.7% |
| 8 | 11/16 · 13/16 | 225 · 198 | 5.1 · 5.3 | 74 · 112 | 24% · 24% | 0% · 0.2% |
| 16 | 24/32 · 25/32 | 337 · 289 | 6.7 · 6.1 | 167 · 121 | 48% · 44% | 1.7% · 0.9% |
| 32 | 25/32 · 53/64 | 319 · 506 | 7.1 · 9.4 | 168 · 187 | 68% · 84% | 5.1% · 14.7% |

Totals: 238 instance runs, 193 resolved (81%), 8,694 LLM calls, no errors, no vLLM preemptions.
Split-sweep N=32 ran only one round (32 runs), so its 32-way load faded after ~10 min; that is why its
throughput is below the single-server value.

---

## 2. Hardware, OS and Slurm

| Item | Value |
|---|---|
| System | TACC Vista, GH200 nodes (`c6xx-xxx`), partitions `gh` (48 h max) and `gh-dev` (2 h max) |
| GPU | NVIDIA H100 96 GB HBM3 (GH200 superchip), driver 590.48.01, CUDA 13.1 (observed on a `gh-dev` node) |
| CPU | 72 × Neoverse-V2 (Grace), 64 KiB host page size |
| Memory | NUMA node 0: 120 GB LPDDR5X with all 72 cores; NUMA node 1: 96 GB HBM3 (the GPU's memory, also visible to Linux, no CPUs) |
| `/dev/kvm` | world read/write on compute nodes (KVM works without root) |
| Local disk | `/tmp`: 261 GB per node |
| Account | `ASC26114` (1 SU per node-hour on `gh` and `gh-dev`, 15-minute minimum per job) |
| Required `#SBATCH` | `-N 1 -n 1 -c 72` (without `-c 72` the job sees 1 CPU), `-A ASC26114` (uppercase) |

CPU placement inside a job:

| Cores | Process |
|---|---|
| 0–7 | vLLM (`taskset -c 0-7`) |
| 8–71 | all microVMs, shared (`taskset -c 8-71`; not pinned per VM) |
| any | agent driver and tenant processes (not pinned) |

Memory placement: vLLM host allocations prefer NUMA node 0 (`numactl --preferred=0`); every VM and the
image staging copy are bound to node 0 (`numactl --membind=0`).

---

## 3. Pinned software versions

| Component | Version / identifier |
|---|---|
| vLLM | 0.30.0, container `vllm/vllm-openai:v0.30.0`, arm64 digest `sha256:4864d46625cbc3307623e29ac742030655e27249feba7b97ec925ce4cc4dfb56` |
| vLLM SIF | `containers/vllm-openai-v0.30.0.sif`, 8.2 GB, sha256 `d78540a2a861a7a69c9749077febfb803d742bd10ebe78fbe1c21c33ede436d9` |
| Apptainer | module `tacc-apptainer/1.4.1` (compute nodes only) |
| Model | `Qwen/Qwen3.8-27B`, revision `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`, BF16 safetensors (18 shards, 52 GB) in `models/Qwen3.8-27B` |
| Dataset | `SWE-bench/SWE-bench_Verified` (split `test`), revision `78f471bf655a3137b2e8a75af1501690ec009ec3` |
| Python (host tools) | 3.12.15 in `env/venv`, managed by uv 0.12.22 |
| mini-swe-agent | 2.4.6 (its packaged `config/benchmarks/swebench.yaml` supplies the prompts) |
| swebench | 5.0.2 (grading) |
| litellm | 1.103.2 |
| Other Python | datasets 5.0.1, huggingface_hub 1.33.0, httpx 0.28.1, openai 2.54.0, pyyaml 6.0.3, numpy 2.5.3, matplotlib 3.11.2 |
| Cloud Hypervisor | v53.0 static aarch64 (`vm/bin/cloud-hypervisor`, sha256 `f192b510eea1c710cbc439d716bb0573c223fc463dbe3e6523788a2b7ef62850`) |
| Guest kernel | `ch-release-v6.16.9-20260508` `Image-arm64` (`vm/kernel/Image`, sha256 `69d1b1235381ec50f1b45cf771a7dff4a9013d452833ab34682d6283e2114010`) |
| Host Python for guard scripts | system `python3` 3.9 (only `slurm/evict_pagecache.py`, `slurm/flush_hbm_pagecache.py`) |

---

## 4. vLLM serving configuration

### 4.1 Launch command (as executed by `slurm/serve_vllm.sh`, defaults expanded)

```bash
module load tacc-apptainer
taskset -c 0-7 numactl --preferred=0 apptainer exec --nv --cleanenv \
  --bind $WORK/sdebench-mt/models:/models \
  --bind /tmp/$USER-vllm-cache:/cache \
  --env HF_HUB_OFFLINE=1,HOME=/cache,VLLM_CACHE_ROOT=/cache/vllm,XDG_CACHE_HOME=/cache,TRITON_CACHE_DIR=/cache/triton,TORCHINDUCTOR_CACHE_DIR=/cache/inductor \
  $WORK/sdebench-mt/containers/vllm-openai-v0.30.0.sif \
  vllm serve /models/Qwen3.8-27B \
    --served-model-name qwen3.8-27b \
    --host 127.0.0.1 --port 8000 \
    --max-model-len 131072 \
    --kv-cache-dtype fp8 \
    --gpu-memory-utilization 0.85 \
    --enable-prefix-caching \
    --reasoning-parser qwen3 \
    --enable-auto-tool-choice --tool-call-parser qwen3_xml \
    --limit-mm-per-prompt '{"image":0,"video":0}' \
    --max-num-seqs 64 \
    --enable-prompt-tokens-details
```

`--enable-prompt-tokens-details` makes every response carry `usage.prompt_tokens_details.cached_tokens`,
which the per-tenant prefix-reuse analysis needs. MTP speculative decoding was **off** (`MTP=0`).

### 4.2 Effective engine configuration (from `vllm.log` of job 1041754)

| Setting | Value | Source |
|---|---|---|
| Weights dtype | `torch.bfloat16`, `quantization=None` | model config |
| Weights on GPU | 50.22 GiB | log |
| KV cache dtype | `fp8` (no calibrated scales; vLLM warns of possible accuracy drop) | flag |
| Max context | 131,072 tokens | flag |
| Tensor / pipeline parallel | 1 / 1 | default |
| Max concurrent sequences | 64 | flag |
| Chunked prefill | on, `max_num_batched_tokens=8192` | default |
| Prefix caching | on | flag |
| Hybrid-model cache mode | Mamba cache mode `align` (auto, because prefix caching is on) | default |
| KV block size | 1568 tokens ("to ensure attention page size >= mamba page size"); mamba page padded 0.13% | auto |
| Attention backend | `FLASH_ATTN` | auto |
| Compilation | `VLLM_COMPILE` (mode 3), CUDA graphs `FULL_AND_PIECEWISE`, capture sizes 1–128 | default |
| `enforce_eager` | False | default |
| Seed | 0 | default |
| KV cache memory | 26.6–26.84 GiB (`Available KV cache memory`) | log |
| KV capacity | 809,733–817,015 tokens per run (32 KiB per token: 16 full-attention layers × K,V × 4 KV heads × 256 dims × 1 byte) | log |
| `nvidia-smi` memory used | 79.4–82.5 GiB, flat (vLLM reserves at start) | `gpu.csv` |
| Startup time | 155–165 s to `/health` OK | job logs |

Model architecture: `Qwen3_5ForConditionalGeneration`, 64 layers (48 linear attention, 16 full
attention, `full_attention_interval=4`), hidden 5120, 4 KV heads, head dim 256. Vision inputs are
disabled by `--limit-mm-per-prompt`.

### 4.3 Sampling

Request parameters sent by the agent (litellm `hosted_vllm/qwen3.8-27b`): `temperature=0.6`,
`top_p=0.95`, `max_tokens=16384`, one bash tool (`tools=[BASH_TOOL]`, auto tool choice).
vLLM fills unspecified parameters from the model's `generation_config.json`, so `top_k=20` also
applied. Thinking mode is the chat template's default (on). Runs are not bit-reproducible: sampling
plus batch-dependent GPU numerics make trajectories differ run to run (see §10).

### 4.4 Startup guard (required on GH200)

`serve_vllm.sh` does the following before each launch, up to 2 attempts:

1. Drops the page cache of our files (`slurm/evict_pagecache.py`: `posix_fadvise(DONTNEED)` on the
   staged VM images and the model files).
2. Flushes other file cache from HBM: `numactl --membind=1 python3 slurm/flush_hbm_pagecache.py 1`
   briefly touches nearly all of NUMA node 1's reclaimable memory, so the kernel drops clean page
   cache there, then exits.
3. Launches vLLM and refuses to continue unless `GPU KV cache size` ≥ `MIN_KV_TOKENS` (780,000).

Why: Linux exposes the GPU's HBM as NUMA node 1, and file cache that lands there counts as used GPU
memory. Without the guard, two jobs (1041921, 1042050) got 2.1–2.2 GiB of KV cache instead of
26.6 GiB and vLLM refused to start.

### 4.5 Knobs (environment variables read by `serve_vllm.sh`)

| Variable | Default | Effect |
|---|---|---|
| `VLLM_TAG` | `v0.30.0` | selects `containers/vllm-openai-$VLLM_TAG.sif` |
| `VLLM_PORT` | 8000 | server port (also used to write `configs/mini.yaml`) |
| `MAX_MODEL_LEN` | 131072 | `--max-model-len` |
| `GPU_UTIL` | 0.85 | `--gpu-memory-utilization` (0.90 fails: only ~83.8 GiB is free at start) |
| `MAX_NUM_SEQS` | 64 | `--max-num-seqs` |
| `TOOL_PARSER` | `qwen3_xml` | `--tool-call-parser` |
| `MTP` | 0 | 1 adds `--speculative-config '{"method":"mtp","num_speculative_tokens":3}'` |
| `PROMPT_DETAILS` | 1 | 0 drops `--enable-prompt-tokens-details` |
| `MIN_KV_TOKENS` | 780000 | KV-capacity floor; lower it (~380000) for a BF16 KV cache |
| `VLLM_CPUS` | `0-7` | `taskset` cores for vLLM |
| `EXTRA_VLLM_ARGS` | empty | appended after all other flags; a repeated flag takes its last value, so `EXTRA_VLLM_ARGS="--kv-cache-dtype auto"` gives a BF16 KV cache (also set `MIN_KV_TOKENS`) |

---

## 5. Agent configuration

- Agent: `minisweagent.agents.default.DefaultAgent` with model `LitellmModel` wrapped as `TimedModel`
  (`sdebench_mt/tenant.py`), which logs every call's duration and token usage.
- Config file: `configs/mini.yaml`, generated at job start by `configs/make_mini_config.py <port>` from
  mini-swe-agent 2.4.6's `config/benchmarks/swebench.yaml`, with these overrides:

| Key | Value |
|---|---|
| `agent.step_limit` | 75 |
| `agent.cost_limit` | 0 (off) |
| `agent.wall_time_limit_seconds` | 3600 |
| `environment.cwd` | `/testbed` |
| `environment.timeout` | 180 s per command |
| `environment.env` | `PAGER=cat MANPAGER=cat LESS=-R PIP_PROGRESS_BAR=off TQDM_DISABLE=1 BASH_ENV=/root/.bashrc` |
| `model.model_name` | `hosted_vllm/qwen3.8-27b` |
| `model.model_kwargs` | `api_base=http://127.0.0.1:8000/v1, api_key=EMPTY, temperature=0.6, top_p=0.95, max_tokens=16384, drop_params=True, timeout=1800, num_retries=2` |
| `model.cost_tracking` | `ignore_errors` (also `MSWEA_COST_TRACKING=ignore_errors`) |

- Environment class: `sdebench_mt/microvm_env.py` (`MicroVMEnvironment`): each action runs as
  `bash -c` inside the tenant's VM over vsock; stdout and stderr are concatenated as the observation.
- Submission: the agent's `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` patch; if empty (step or time
  limit), the working-tree diff `git add -A && git diff --cached HEAD` in `/testbed` is used.

---

## 6. Sandbox (microVM) configuration

| Item | Value |
|---|---|
| VMM | Cloud Hypervisor v53.0, one process per instance run, unprivileged |
| Guest | 2 vCPU, 4096 MiB RAM (`--vm-vcpus 2 --vm-mem-mb 4096`) |
| Kernel cmdline | `console=ttyAMA0 root=/dev/vda ro rootfstype=ext4 init=/sbin/replay-init quiet` |
| Disks | `vda` = instance rootfs ext4, `readonly=on,image_type=raw` (shared, staged to `/tmp/$USER-images`); `vdb` = private 8 GiB sparse ext4 upper layer, `mkfs.ext4 -b 4096` per run |
| Root filesystem | guest init mounts overlayfs (lower `/`, upper on `vdb`) and chroots into it |
| Network | none (no tap device without root); vsock only, guest CID 3, port 52 |
| Guest agent | `/usr/local/bin/replay-agent` (Python, from `vm/guest/replay-agent`): one JSON line per request — `{"op":"exec","cwd","command","timeout","env"}` → `{"exit_status","stdout","stderr","duration_ms"}`; also `write_file`, `ping` |
| Host side | `sdebench_mt/vmm.py` (`MicroVM`): connect to the vsock Unix socket, send `CONNECT 52`, then JSON lines |
| Boot time | 0.50 s to agent ready |
| Serial / VMM logs | `/tmp/$USER-vms/<name>/serial.log`, `vmm.log` (deleted when the run ends) |

Instance root filesystems (`vm/images/<instance>.ext4`) are built by `vm/build_instance.sh`:
1. `apptainer build --sandbox` from `docker://swebench/sweb.eval.arm64.<id>:latest` (`__` → `_1776_`,
   lowercase), with a private `APPTAINER_CACHEDIR` per build.
2. Install `replay-agent` (shebang set to `/opt/miniconda3/bin/python3`) and `vm/guest/init` as
   `/sbin/replay-init`; append `[safe] directory = *` to `/etc/gitconfig`; create mount points.
3. `vm/sandbox_to_ext4.sh`: strip setuid/setgid bits, then `mkfs.ext4 -b 4096 -d <sandbox>` on
   node-local `/tmp`, size = content × 1.3 + 3 GiB, copied sparsely to `vm/images/`.

---

## 7. Workload

19 SWE-bench Verified instances (`configs/instances_ok.txt`, in this order — the order matters, see §8),
each with an official arm64 eval image whose gold patch resolves offline in a microVM:

| # | Instance | arm64 image digest (`swebench/sweb.eval.arm64.<id>:latest`) |
|---|---|---|
| 1 | django__django-10973 | `sha256:9c3edcd04cdf5bd5a5ab4a448c8b53e250759da3ebb50e1e721a16cf1b17e75d` |
| 2 | django__django-11749 | `sha256:4e2e8a090abe76d3a504e853beed7c6843ec4b90b33109abe79ca80564b330f7` |
| 3 | django__django-12262 | `sha256:0331a358583b8c1caab1fe24d1677b51c9ed0779187cb3a29dfcdf24a62a1324` |
| 4 | django__django-13112 | `sha256:b5ab9e37318b407adec12b9eb5a9342299c89bf3e15e773074568d5032eb7cb9` |
| 5 | django__django-13821 | `sha256:2d5e8e7549753ac3e87bc0bff03a67c68ed4e5ffc98b0d6e09d0108689abd1a3` |
| 6 | django__django-14787 | `sha256:9e3ba20133162c163fcfd2c1765c9743a533ebcade00343e09cbf905d8cac5d3` |
| 7 | django__django-15380 | `sha256:7d1a208b3e332b97f1b1ce95cf9db5a3cadc60ecf362cfc046d56b83313b1b9d` |
| 8 | django__django-16255 | `sha256:7eb27b351720d1a30577ef530056fbef0136c607357d8e98244a52a76ffd377c` |
| 9 | sympy__sympy-11618 | `sha256:471ed97c29e418d4da58fa6adf78ab13681247bf33e4d7ea86a0acec2a162e6c` |
| 10 | sympy__sympy-13757 | `sha256:d663e5f364e61e08e78e214f071351696f07f514c3e66fee807bc96eedfaafb6` |
| 11 | sympy__sympy-15875 | `sha256:1eea9322aa00757539774910050280baf0ae9940fafaabf0a4f823fc86d31928` |
| 12 | sympy__sympy-18211 | `sha256:88773728ca271e92a85a5a46431f662144e03ec186d141e02d8156aac8f1a095` |
| 13 | sympy__sympy-20916 | `sha256:8cf96844ddc9d52e55ca9a8c0a7ad3222e7314701ef13b5bf63f48a4650949b9` |
| 14 | sympy__sympy-23262 | `sha256:49c5ec72e66de9e178d85248f037e135c046f7df9ad722887ac9855d7aa8a1fd` |
| 15 | pytest-dev__pytest-10051 | `sha256:33d8bfe03be9fd430a2182330b9a7ace5868851a0f1ce13ec6882813c6c80b45` |
| 16 | pytest-dev__pytest-5631 | `sha256:74a596fa08fb7b47eb993581947c1ffb5c6d42e9f6830dea1b54b4c307a205e7` |
| 17 | pytest-dev__pytest-6202 | `sha256:8c8ec01f338884af403532807d8535bded91e03429a84da091f015326753cff6` |
| 18 | pylint-dev__pylint-6528 | `sha256:0d8a977e2c866e223ab1dd1a873a10536513fe0ea8abc04d59eb0142ef8b2d6b` |
| 19 | astropy__astropy-12907 | `sha256:6373496b634a2e95077bfa3407ea8d334fd3520439832f003a0d631ab8a0d348` |

All digests were last updated on Docker Hub on 2025-04-20, before the images were pulled
(2026-10-01). To pin exactly, replace `:latest` in `vm/build_instance.sh` with `@<digest>`.

Excluded from the original 24-instance selection: 4 sphinx instances (swebench cannot parse their tox
output), pylint-dev__pylint-4661 (its eval needs `pip install` from the network), and psf/requests
instances (tests call httpbin.org). Instance metadata (problem statement, eval script, tests) is
frozen in `configs/instances.jsonl`; gold-patch results are in `configs/gold_check.jsonl`.

Grading (`sdebench_mt/swebench_eval.py`): in the same VM after the agent finishes, `git reset --hard
<base_commit> && git clean -fdq`, apply the patch with swebench's `GIT_APPLY_CMDS` fallback sequence,
run the instance's `eval.sh` (timeout 1800 s), grade with `swebench.harness.grading.get_eval_report`.

---

## 8. Load generator and measurement

`sdebench_mt/driver.py -n N --jobs J --out <dir>`:

- Closed loop: N tenant processes (`multiprocessing` spawn) pull instance runs from one shared queue.
- Queue order: `ids[i % 19]` for i = 0..J−1 over `configs/instances_ok.txt` (round-robin in list order;
  this is why small N saw only django instances).
- Tenant starts staggered by 5 s (`--stagger-s 5`).
- Admission control: a tenant waits before starting an instance while NUMA node 0's
  (MemFree + Inactive(file)) / MemTotal < 0.10 (`--min-mem-frac 0.10`).
- Per instance run: boot VM → agent → collect patch → grade in VM → stop VM.
- Split sweep only: `--dispatch-deadline` = job start + 85 min (no new instance after that).

Recorded per N (`N<n>/`):

| File | Content |
|---|---|
| `results.jsonl` | one row per instance run: tenant, boot/agent/eval seconds, LLM calls, exit status, resolved, `vmm_pid` |
| `tenants/t<NNN>/<id>.events.jsonl` | every LLM call (start, duration, prompt/completion/cached/reasoning tokens) and tool command (duration, return code, output size) |
| `tenants/t<NNN>/<id>.traj.json`, `.patch`, `.result.json` | trajectory, submitted patch, result |
| `samples.jsonl` | 1 Hz: vLLM `/metrics` (running/waiting, `kv_cache_usage_perc`, prefix-cache hits/queries, token counters, TTFT/queue/prefill/decode sums and counts, preemptions), host MemAvailable, NUMA-0 MemFree, per-VMM RSS and CPU ticks |
| `gpu.csv` | 1 Hz `nvidia-smi`: utilization, memory used, power, SM clock |
| `vllm.log` | split sweep: per N; single-server sweep: in the run root |

Analysis: `env/venv/bin/python sdebench_mt/analyze.py <run dir>` → `summary.md`, `summary.json`,
`sweep.png`; `sdebench_mt/plot_memory.py <run dir>` → `memory.png`. A call counts as a prefix-cache
miss when `cached_tokens` falls short of the previous call's prompt rounded down to a 1568-token block
by at least one block. The block remainder (previous prompt mod 1568) is reported separately as
"block tail".

---

## 9. Reproduction steps

### 9.1 One-time setup (login node, then compute jobs)

```bash
export MT=$WORK/sdebench-mt && mkdir -p $MT && cd $MT
# Python env (login node: limit threads or uv panics)
export RAYON_NUM_THREADS=2 UV_CONCURRENT_BUILDS=1 UV_CACHE_DIR=$MT/env/uv-cache UV_PYTHON_INSTALL_DIR=$MT/env/python
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=$MT/env/bin INSTALLER_NO_MODIFY_PATH=1 sh
env/bin/uv venv -p 3.12 env/venv
VIRTUAL_ENV=env/venv env/bin/uv pip install mini-swe-agent==2.4.6 swebench==5.0.2 litellm==1.103.2 \
  datasets==5.0.1 "huggingface_hub[cli]==1.33.0" httpx==0.28.1 matplotlib==3.11.2 pandas aiohttp pyyaml
# Model, pinned revision (52 GB)
HF_HOME=$MT/models env/venv/bin/hf download Qwen/Qwen3.8-27B \
  --revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 --local-dir models/Qwen3.8-27B --max-workers 4
# Cloud Hypervisor and guest kernel
mkdir -p vm/bin vm/kernel vm/images
R=https://github.com/cloud-hypervisor
curl -fsSL -o vm/bin/cloud-hypervisor $R/cloud-hypervisor/releases/download/v53.0/cloud-hypervisor-static-aarch64
curl -fsSL -o vm/bin/ch-remote $R/cloud-hypervisor/releases/download/v53.0/ch-remote-static-aarch64
curl -fsSL -o vm/kernel/Image $R/linux/releases/download/ch-release-v6.16.9-20260508/Image-arm64
chmod +x vm/bin/*
cd runs
sbatch ../slurm/probe.slurm         # optional: KVM check, base image, VM boot + vsock round trip
sbatch ../slurm/build_images.slurm  # vLLM SIF pull, configs/instances.jsonl, 24 images, gold check
```

`build_images.slurm` pulls `vllm/vllm-openai:v0.30.0`; for an exact match pull by digest
(`docker://vllm/vllm-openai@sha256:4864d466…`) and check the SIF sha256 in §3. It writes
`configs/instances_ok.txt` (the 19 instances of §7) from the gold check.

### 9.2 Smoke test (gh-dev, ~45 min)

```bash
cd $WORK/sdebench-mt/runs && sbatch ../slurm/serve_smoke.slurm
```
Expect: a bash tool call in the chat sanity check, N=1 completing one instance end to end
(`django__django-10973`; it resolved in 11 of 18 runs overall), and N=4 ending with
`max n_vmm 4 max running 4.0`.

### 9.3 Single-server sweep (gh, ~6 h, ~6 SU)

```bash
cd $WORK/sdebench-mt/runs && sbatch ../slurm/sweep.slurm
# options: NS="1 2 4 8 16 32" (default), JOBS_PER_N=<n> (default 8 for N<8, else 2N)
```
Output: `runs/sdebench-sweep_<jobid>/`, analysis written at the end.

### 9.4 Split sweep (gh-dev, one 2 h job per N)

`gh-dev` allows 1 running and 3 submitted jobs per user (a job in COMPLETING still counts), and TACC
rejects `sbatch` from compute nodes, so submit from a login node as slots free up:

```bash
cd $WORK/sdebench-mt/runs
SID=$(date +%Y%m%d-%H%M)
for i in 0 1 2; do sbatch --export=ALL,SWEEP_ID=$SID,IDX=$i ../slurm/sweep_dev.slurm; done
# later, when fewer than 3 of your gh-dev jobs remain:
sbatch --export=ALL,SWEEP_ID=$SID,IDX=3 ../slurm/sweep_dev.slurm   # then IDX=4, IDX=5
```
IDX 0–5 = N 1, 2, 4, 8, 16, 32 with 8, 8, 8, 16, 32, 32 runs (`CHUNKS`, `JOBS` override). A rerun of
an N whose `summary.json` exists is skipped. Results: `runs/sdebench-sweep-dev_$SID/`.

---

## 10. Verification checklist

In each job's `vllm.log` / Slurm output, expect:

- `version 0.30.0`
- `Mamba cache mode is set to 'align'`
- `Chunked prefill is enabled with max_num_batched_tokens=8192`
- `Using FLASH_ATTN attention backend`
- `Model loading took 50.22 GiB`
- `Setting attention block size to 1568 tokens`
- `Available KV cache memory: 26.6`–`26.84 GiB`
- `GPU KV cache size: 809,733`–`817,015 tokens`
- `KV cache tokens: <value> (minimum 780000), attempt 1`
- `vLLM healthy after 150–165s`

Expected run-to-run variation, from comparing the two sweeps:

- Generation throughput and median call latency agree within ~17% at N ≤ 16.
- p95 latency varies up to 2×; it is driven by a few long reasoning outputs.
- Resolve rate varies by ±1–2 instances per cell; the pooled rate was 78% (split) and 83% (single-server).
- N=32 depends on how long the 32-way load lasts: one round versus two gave 319 and 506 tok/s, and 5%
  versus 15% prefix-cache misses.

---

## 11. Known pitfalls on Vista

| Symptom | Cause | Fix used |
|---|---|---|
| Job sees 1 CPU | `-n 1` alone | add `#SBATCH -c 72` |
| `apptainer: command not found` / refused | Apptainer is compute-node only | run inside a job |
| vLLM: "permission denied" mounting cache | bind onto `/root` in the container | bind to `/cache`, set `HOME=/cache` |
| vLLM: free memory below 0.9 × 95 GiB | ~11 GiB of HBM already in use at start | `--gpu-memory-utilization 0.85` |
| vLLM: KV cache ~2 GiB, refuses to start | file page cache in HBM (NUMA node 1) | startup guard, §4.4 |
| `CHAIN_SUBMIT_FAILED`, "Job submission is not allowed from this host" | no `sbatch` from compute nodes | submit from the login node |
| apptainer builds hang at "Fetching OCI image" | concurrent builds sharing one cache dir | private `APPTAINER_CACHEDIR` per build |
| apt fails in `--fakeroot` builds (setgroups) | no `/etc/subuid` entry; root-mapped namespace | use prebuilt images; no `%post` apt |
| `mkfs.ext4`: "too small for device" | Lustre reports 4 MiB as sector size | build ext4 on `/tmp`, then copy |
| Guest kernel panic "bad block size 65536" | host 64 KiB pages → 64 KiB ext4 blocks | `mkfs.ext4 -b 4096` |
| Guest `mount`: "must be superuser" | setuid bits on files owned by the build uid | strip setuid/setgid before packing |
| uv/rust panics on login node | thread limits | `RAYON_NUM_THREADS=2` |
| `srun --jobid=… --overlap` rejected | TACC filter requires `-p` and `-t` | add `-p gh-dev -A ASC26114 -t 00:05:00` |

---

## 12. Differences between the recorded runs

- Split sweep N=1 and N=2 (jobs 1041919, 1041920) ran before the startup guard of §4.4 existed; they
  still got full KV capacity (809,733 and 811,190 tokens). Every other job ran with the scripts exactly
  as they are now.
- Split-sweep N=4 and N=8 are reruns (jobs 1042100, 1042568) after the first attempts (1041921,
  1042050) failed at vLLM startup; no tenant ran in the failed attempts.
- The single-server sweep kept one vLLM server and its prefix cache across all N; the split sweep
  started a fresh server per N.
- `analyze.py` was revised after the runs to make the miss metric block-aware; re-run it on any
  directory to regenerate `summary.md` with the current definitions.

## 13. File map

| Path | Purpose |
|---|---|
| `slurm/serve_vllm.sh` | vLLM launch, startup guard, KV floor |
| `slurm/common.sh` | image staging, `mini.yaml`, vLLM start, cleanup trap |
| `slurm/sweep.slurm`, `slurm/sweep_dev.slurm` | single-server and split sweeps |
| `slurm/serve_smoke.slurm`, `slurm/build_images.slurm`, `slurm/probe.slurm` | smoke test, image build, KVM probe |
| `slurm/evict_pagecache.py`, `slurm/flush_hbm_pagecache.py` | page-cache guard helpers |
| `sdebench_mt/` | `driver.py`, `tenant.py`, `vmm.py`, `microvm_env.py`, `swebench_eval.py`, `metrics.py`, `analyze.py`, `plot_memory.py`, `gold_check.py` |
| `vm/` | CH binaries, kernel, guest agent and init, image build scripts, `images/` |
| `configs/` | `instances.txt`, `instances_ok.txt`, `instances.jsonl`, `gold_check.jsonl`, `make_mini_config.py`, `mini.yaml` |
| `containers/` | `vllm-openai-v0.30.0.sif` |
| `models/` | Qwen3.8-27B weights and the HF dataset cache |
| `runs/` | job outputs (§1) |
