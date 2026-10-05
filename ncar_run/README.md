# ncar_run: SDEBench multi-tenancy with KV-cache migration on a Chameleon CHI@NCAR GH200

Everything needed to reproduce sweep `20261003-0506` (3 Oct 2026) except the large inputs
(weights, container, instance images) and credentials. The authoritative setup record is
`chiconfig.md`; the results are in its §8.2 and in `results/`.

**What was run:** N = 16 and 32 concurrent SWE-bench Verified agent tenants (mini-swe-agent 2.4.6, one
Cloud Hypervisor microVM per instance run) sharing one Qwen3.8-27B vLLM 0.30.0 server, in three arms:

| Arm | vLLM flags beyond the Vista recipe (`chameleon/env.sh` → `arm_args`) |
|---|---|
| `baseline` | `--kv-cache-memory-bytes 28668876800` (GPU KV pinned to 26.7 GiB = 811,190 tokens) |
| `offload` | same + `--kv-offloading-backend native --kv-offloading-size 64` (64 GiB CPU KV cache in Grace memory) |
| `uva` | `--kv-cache-memory-bytes 37258811392 --cpu-offload-gb 8` (8 GiB of weights read in place over NVLink-C2C) |

The base launch command (all arms) is `sdebench-mt/slurm/serve_vllm.sh`: `--max-model-len 131072
--kv-cache-dtype fp8 --gpu-memory-utilization 0.85 --enable-prefix-caching --max-num-seqs 64
--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_xml
--limit-mm-per-prompt '{"image":0,"video":0}' --enable-prompt-tokens-details`. Fresh server per
(arm, N); 2N instance runs per N.

## Layout

| Path | Content |
|---|---|
| `chiconfig.md` | Setup record: resources, hardware, pinned versions, vLLM config and offload policy (§4), code changes vs Vista (§5), object storage (§6), smoke tests (§8), **results (§8.2)**, procedures (§9), pitfalls (§11) |
| `ghconfig.md` | The original TACC Vista guide this reproduces (reference values in its §1) |
| `HANDOFF.md` | Node hand-off written 3 Oct 2026: access, hardware, environment, storage commands (the node is released; IPs are historical) |
| `api-list` | CHI@NCAR service endpoints |
| `sdebench-mt/` | The experiment tree, code and configs only (see Excluded) |
| `sdebench-mt/chameleon/` | Chameleon scripts: `env.sh` (arm flags, pins), `setup_node.sh`, `install_apptainer.sh`, `build_images.sh`, `store.sh`, `smoke.sh`, `offload_check.py`, `run_arm.sh`, `sweep.sh`, `phase2.sh`, `prefill_cost.py`, `recompute.py`, `timeline.py`, `compare.py`, `synth_load.py`, `synth.sh`, `bin/module` (no-op stand-in for TACC `module`) |
| `sdebench-mt/slurm/` | Vista scripts, run unchanged except `serve_vllm.sh` (+ `HEALTH_TIMEOUT_S` knob) |
| `sdebench-mt/sdebench_mt/` | Driver, tenant, microVM manager, metrics sampler (+ NUMA and vLLM-tree memory), evaluation, analysis |
| `sdebench-mt/configs/` | **Exact inputs:** `instances_ok.txt` (19 instances, in order), `image_digests.txt` (Docker image digests), `instances.jsonl` (SWE-bench Verified rows at revision `78f471bf`), `mini.yaml` (the generated agent config: step limit 75, T=0.6, top_p 0.95, max_tokens 16384), `gold_check.jsonl` + `gold/` (19/19 gold patches resolved) |
| `sdebench-mt/vm/` | microVM pieces: `guest/init` (overlay root), `guest/replay-agent` (vsock exec server), `build_instance.sh` (image from the digest), `sandbox_to_ext4.sh`, `base.def` |
| `node-bin/` | `weights.sh` (weights ↔ object store), `serve.sh` (ad-hoc vLLM from pip), `bench.sh` (latency benchmark) |
| `results/sweep-20261003-0506/` | Per arm: `summary.md/json` (analyze.py), `recompute.md/json`, `timeline_summary.json`, `sweep.png`; per N: `arm.json` (exact flags), `summary.json`, `results.jsonl` (one row per instance run), `calls_recompute.csv` (one row per LLM call), `prefill_cost.json` (calibration), `timeline.csv/png`; `compare_20261003-0506.md` (all cells side by side) |
| `results/vllm-bench-20261002/` | `vllm bench serve` latency/throughput of the same server config (JSON + GPU telemetry) |
| `logs/` | Node logs of the actual runs (`sweep`, `phase2`, smoke tests, synthetic validation, setup, Apptainer build), with `.txt` added because the repo ignores `*.log` |

## Excluded, and how to get it

| Item | Size | How |
|---|---|---|
| Qwen3.8-27B BF16 weights, revision `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0` | 52 GB | `hf download Qwen/Qwen3.8-27B --revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 --local-dir ~/models/Qwen3.8-27B`, or `node-bin/weights.sh pull` from the CHI@NCAR object store (project members) |
| vLLM container `vllm/vllm-openai@sha256:4864d46625cbc3307623e29ac742030655e27249feba7b97ec925ce4cc4dfb56` (arm64, v0.30.0) | 8.2 GB | pulled by `chameleon/build_images.sh` (or `store.sh pull`) |
| 19 instance rootfs images | 43 GB on disk | built by `chameleon/build_images.sh` from `configs/image_digests.txt` (about 25 min), or `store.sh pull` |
| Cloud Hypervisor v53.0 + guest kernel `ch-release-v6.16.9-20260508` | 29 MB | downloaded and sha256-checked by `chameleon/setup_node.sh` |
| Python env (`env/venv`) and pip vLLM (`~/vllm-env`) | | created by `setup_node.sh` with the pinned versions in `chiconfig.md` §3 |
| Raw runs (`sdebench-mt/runs/`: 1 Hz samples, per-call events, trajectories, patches, vLLM logs) | 315 MB, 1,348 files | object store `chi-ncar:sdebench-mt/runs/` (project members) or on request |
| Credentials (`clouds-ncar.yaml`, `rclone.conf`, SSH key) | | never in this repo; create an application credential for the CHI@NCAR project and an rclone `swift` remote (`chiconfig.md` §6) |

## Reproduce

1. Lease a `gpu_gh200_96gb` node at CHI@NCAR and launch `CC-Ubuntu24.04-CUDA-ARM64` with your keypair
   (`chiconfig.md` §9.1; x86 images do not boot on GH200).
2. On the node: copy `sdebench-mt/` to `~/sdebench-mt`, get the weights, then
   `bash chameleon/setup_node.sh` (Apptainer build, Python env, Cloud Hypervisor) and
   `bash chameleon/build_images.sh` (container, instance images, gold check).
3. `bash chameleon/smoke.sh offload` (expect `OFFLOAD_CHECK … MIGRATED BACK FROM CPU`).
4. `SWEEP_ID=<id> ARMS="offload baseline uva" NS="16 32" setsid nohup bash chameleon/sweep.sh > ~/sweep.log 2>&1 < /dev/null &`
   (about 1.5–2.5 h per (arm, N); vLLM takes about 14 min to become healthy here).
5. `python3 chameleon/compare.py <id>` and compare with `results/sweep-20261003-0506/compare_20261003-0506.md`.
   Expected run-to-run variation: `ghconfig.md` §10.

Headline results (one run per cell): at N=32 the baseline lost cached blocks on 19.1% of follow-up calls
(eviction recompute 10.9% of GPU time); with the 64 GiB CPU KV cache that fell to 0.1% (0.09%),
generation throughput rose 431 → 467 tok/s and median LLM latency fell 10.3 → 8.9 s. The `uva` arm
was a net loss (decode about 2.4× slower). Details: `chiconfig.md` §8.2.
