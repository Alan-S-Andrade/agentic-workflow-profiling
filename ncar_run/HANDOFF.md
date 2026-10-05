# Hand-off: CHI@NCAR GH200 node `gh200-dev-probe`

As of 2026-10-03 21:06 UTC. The node is idle. All results, code, model weights and docs are saved
in the object store (§6); nothing on this node needs to be kept. This file lives at `~/HANDOFF.md`
on the node, with copies at `chi-ncar:sdebench-mt/HANDOFF.md` and on the workstation at
`/home/jc/chi-ncar/HANDOFF.md`.

## 1. Status

| Item | Value |
|---|---|
| Lease | `ghprobe` (`f0d1008e-c244-4430-ae4f-2f89d2ad36ce`), ACTIVE, 1 × `gpu_gh200_96gb`, **ends 2026-10-06 01:00 UTC** |
| Instance | `gh200_dev_probe` (`ab7075bf-46cf-469e-9367-44ff646e09e7`), ACTIVE, up since 2026-10-02 16:40 UTC |
| Image | `CC-Ubuntu24.04-CUDA-ARM64` (`bdd2abf4-476d-45ed-bcd1-4192f192ebaf`) |
| Activity | idle: GPU 1 MiB used and 0% busy, no vLLM server, no microVMs, no jobs |
| Last work | sweep `20261003-0506` (offload, baseline and uva arms × N=16/32) finished 15:38 UTC; results synced and verified |
| Disk | `/` 205 GB used, 598 GB free |

When the lease ends, the instance is deleted and its disks are wiped. The floating IP stays
allocated to the project until it is released (§2).

## 2. Access

- **SSH:** `ssh -i /home/jc/liveremote/keys/chi_ncar_gh200 -o UserKnownHostsFile=/home/jc/liveremote/keys/known_hosts cc@128.117.250.91`. User `cc` has passwordless sudo.
  - The private key is **not on this node**. It is on the workstation in the shared host registry,
    `/home/jc/liveremote/keys/chi_ncar_gh200` (moved there 2026-10-03; `/home/jc/chi-ncar/chi-ncar-key` is now a symlink to it).
    That registry's `keys/known_hosts` holds this node's verified host keys.
  - The full registry entry is `gh200-dev-probe` in `/home/jc/liveremote/hosts.json` (no secrets stored there).
  - The OpenStack keypair is `chi-ncar-key` (fingerprint `78:a5:4f:82:86:06:12:a2:9d:59:61:56:ae:a3:a9:e8`).
- **Network:**
  - Floating IP 128.117.250.91 on `sharednet1` (fixed 172.20.0.249); also `fabnetv4` 10.191.132.176.
  - The project's `default` security group allows TCP 22 from 0.0.0.0/0, plus all traffic between
    members of the group. Consider narrowing the SSH rule.
- **vLLM (when running)** listens only on 127.0.0.1:8000. Reach it through a tunnel:
  `ssh -i /home/jc/liveremote/keys/chi_ncar_gh200 -N -L 8000:localhost:8000 cc@128.117.250.91`.
- **Console:** serial only, via `openstack console url show --serial ab7075bf-46cf-469e-9367-44ff646e09e7`.
  There is no VNC and no console log.
- **OpenStack CLI** (run on the workstation: python-openstackclient + python-blazarclient):
  ```bash
  export OS_CLIENT_CONFIG_FILE=/home/jc/chi-ncar/clouds-ncar.yaml OS_CLOUD=openstack
  openstack reservation lease show ghprobe
  openstack reservation lease set --end-date "2026-10-07 01:00" ghprobe   # extend (or --prolong-for), if capacity allows
  openstack floating ip delete 128.117.250.91                             # release the IP when done
  ```
  - The credential in `clouds-ncar.yaml` is the restricted application credential `ncar_id` (roles
    member + reader), expiring 2027-09-01.
  - Site: CHI@NCAR, identity `https://chi.hpc.ucar.edu:5000`, project
    `12b223bd6fb04c30a33bf45033f2f484`.

## 3. Hardware

| Part | Detail |
|---|---|
| System | Quanta QuantaGrid S74G-2U, 1 × NVIDIA GH200 superchip |
| CPU | 72 × Arm Neoverse-V2 (Grace), 1 socket, 4 KiB pages |
| CPU memory | NUMA node 0: 471 GiB LPDDR5X (all cores) |
| GPU | GH200 480GB = H100 with 96 GB HBM3. The HBM also appears to Linux as NUMA node 1 (95 GiB, no CPUs). ATS addressing and C2C mode are enabled |
| CPU↔GPU link | NVLink-C2C. vLLM KV copies measured 133 GB/s (to GPU) and 66 GB/s (to CPU) |
| Disks | 894 GB NVMe (`/`, holds everything). 1.7 TB SAS SSD `/dev/sda`, unformatted and unused |
| Other | `/dev/shm` 236 GiB tmpfs; 2 × ConnectX-7; KVM available (`cc` has rw on `/dev/kvm` via ACL) |

## 4. Software

| Component | Version / location |
|---|---|
| OS | Ubuntu 24.04.5, kernel 6.8.0-142-generic |
| NVIDIA | driver 580.178.04 (CUDA 13.0); host toolkit CUDA 12.6 in `/usr/local/cuda` |
| Apptainer | 1.4.1, built from source (`/usr/local/bin`); squashfuse; AppArmor userns profile |
| vLLM | experiment runs: container `~/sdebench-mt/containers/vllm-openai-v0.30.0.sif` (pinned digest). Ad-hoc serving: pip vLLM 0.30.0 in `~/vllm-env` (torch 2.13 cu130) |
| Python | `~/sdebench-mt/env/venv` (3.12.15: mini-swe-agent 2.4.6, swebench 5.0.2, litellm 1.103.2, …), managed by uv 0.12.22 (`~/.local/bin`) |
| Model | `~/models/Qwen3.8-27B`: BF16, revision `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`, 52 GB. `~/sdebench-mt/models` is a symlink to `~/models` |
| Sandbox | Cloud Hypervisor v53.0 + guest kernel in `~/sdebench-mt/vm/`; 19 instance images in `~/sdebench-mt/vm/images/` |
| Tools | rclone 1.69.1, numactl, zstd |

## 5. Layout on the node

| Path | What |
|---|---|
| `~/sdebench-mt/` | experiment repo: `chameleon/` (scripts), `slurm/`, `sdebench_mt/`, `configs/`, `chiconfig.md` (full setup record), `ghconfig.md` (Vista original) |
| `~/sdebench-mt/runs/` | all runs: 3 sweep arms, 6 smoke tests, 2 synthetic runs, `compare_20261003-0506.md` |
| `~/sdebench-mt/vm/images/`, `vm/images.zst/` | instance images (116 GB apparent, 43 GB on disk), plus their compressed copies |
| `~/bin/` | `weights.sh` (model weights to and from the object store), `serve.sh` (ad-hoc vLLM), `bench.sh` (latency benchmark) |
| `~/bench/` | vLLM latency benchmark results (2026-10-02) |
| `~/*.log` | logs of setup, smoke tests, sweep (`sweep.log`), phase 2 (`phase2.log`) |
| `~/sdebench-mt-next/`, `/tmp/cc-images` (43 GB), `/tmp/cc-apcache` (24 GB) | staging copies and caches, safe to delete |

## 6. Persistent storage (object store)

The Swift object store (Ceph RGW, `https://chi.hpc.ucar.edu:7480`) is the only persistent storage at
CHI@NCAR: there are no volumes and no shared filesystems. It is reachable from the node and from the
workstation.

**Credentials.** The rclone remote `chi-ncar` (swift backend + the application credential) is defined in
`~/.config/rclone/rclone.conf` (mode 0600). It contains the credential secret, so don't copy it
anywhere public. A copy is on the workstation at `/home/jc/chi-ncar/rclone.conf`. To set up a new node:
```bash
scp -i chi-ncar-key rclone.conf cc@<ip>:~ && ssh -i chi-ncar-key cc@<ip> 'install -Dm600 ~/rclone.conf ~/.config/rclone/rclone.conf && rm ~/rclone.conf'
```

**What is stored** (2026-10-03):

| Container / path | Content | Size |
|---|---|---|
| `llm-weights/Qwen/Qwen3.8-27B/` | Qwen3.8-27B BF16 weights + `REVISION` | 51.8 GiB |
| `sdebench-mt/code/` | the repo incl. `chameleon/`, `chiconfig.md` | 0.5 MiB |
| `sdebench-mt/containers/` | `vllm-openai-v0.30.0.sif` | 7.7 GiB |
| `sdebench-mt/images/` | 19 instance images, zstd-compressed | 17.2 GiB |
| `sdebench-mt/vm/` | cloud-hypervisor, ch-remote, guest kernel | 29 MiB |
| `sdebench-mt/runs/` | every run (1,348 files) | 310 MiB |
| `sdebench-mt/bench-vllm/` | latency benchmark | 0.3 MiB |
| `sdebench-mt_segments` | segments of the 8.2 GB SIF; rclone manages it, **do not delete** | 7.7 GiB |

**Store and fetch:**
```bash
rclone lsd chi-ncar:                                              # containers
rclone ls  chi-ncar:sdebench-mt/runs | head                       # objects
rclone copy ./results chi-ncar:sdebench-mt/runs/my-run --progress # store (dir or file)
rclone copy chi-ncar:sdebench-mt/runs/my-run ./results --progress # fetch
rclone check ./results chi-ncar:sdebench-mt/runs/my-run --one-way # verify by MD5
~/bin/weights.sh push|pull|check                                  # Qwen weights <-> ~/models/Qwen3.8-27B
~/sdebench-mt/chameleon/store.sh push|pull|push-runs|ls           # experiment code, SIF, images, runs
```
- From the workstation: `rclone --config /home/jc/chi-ncar/rclone.conf ls chi-ncar:sdebench-mt`, or
  `openstack object list sdebench-mt`.
- **Limits:** 5 GiB per object. rclone splits larger files into `<container>_segments` by itself;
  `openstack object create` stops at about 4 GB. There are at most 1,000 containers per project and no
  byte quota is set.
- **Durability:** data is kept as 2 copies in one data center, with no backup elsewhere.
- **Speed measured from this node:** about 100 MB/s up and 270 MB/s down. The 52 GB of weights take
  3.5 min to fetch and 8.5 min to store.

## 7. Common tasks

- **Serve Qwen ad hoc:** `~/bin/serve.sh` (pip vLLM, 127.0.0.1:8000, about 14 min to become ready). Stop it before experiments.
- **Smoke test:** `cd ~/sdebench-mt && bash chameleon/smoke.sh offload` (`QUICK=1` for start-up and the migration check only).
- **Run the experiment:**
  `cd ~/sdebench-mt && setsid nohup bash chameleon/sweep.sh > ~/sweep.log 2>&1 < /dev/null &`
  Env: `ARMS="offload baseline uva"`, `NS="16 32"`, `SWEEP_ID`. A finished (arm, N) is skipped.
  Each (arm, N) takes 1.5–2.5 h, including about 14 min of server start-up.
- **Compare arms:** `python3 chameleon/compare.py <SWEEP_ID>`. Per-run outputs: `summary.md`,
  `recompute.md`, `N<n>/timeline.png`.
- **Back up results:** `bash chameleon/store.sh push-runs` (sweep.sh does this at the end).
- **New lease from scratch** (ARM64 image, keypair at launch, restore from the object store):
  `~/sdebench-mt/chiconfig.md` §9.1.

## 8. Gotchas

- GH200 is aarch64: use a `*-ARM64` image. x86 images go ACTIVE but never boot.
- Nova cannot add a key to a running server. Launch with `--key-name chi-ncar-key`; otherwise the only
  fix is a rebuild, which wipes the disk.
- vLLM needs about 13–14 min to become healthy; weight loading alone takes 6–8 min.
- The experiment's cleanup kills every `vllm serve`, so don't run another vLLM at the same time.
- Start long jobs with `setsid nohup … < /dev/null &`, or the SSH session hangs.
- Don't use `pkill -f <pattern>` when the pattern also appears in your own command line.
- pip vLLM outside the container needs `VLLM_USE_FLASHINFER_SAMPLER=0` (`serve.sh` sets it): FlashInfer
  compiles its sampler at first use and fails on this node's nvcc/header mismatch.
- File page cache can land in HBM (NUMA node 1) and shrink GPU memory. `slurm/serve_vllm.sh` evicts it
  before each start.

## 9. References

- Setup record and results: `~/sdebench-mt/chiconfig.md` (§8.2 results, §9 procedures).
- Results report: https://claude.ai/code/artifact/b10e972e-1a2b-47c6-ad45-e01e57f1f7e4
- KV-offload policy diagrams: https://claude.ai/artifact/JUYMx1iAwUA7NqLQY2TdMb
- Workstation copies: `/home/jc/chi-ncar/` (credentials, `chiconfig.md`, `sweep-results/`, full runs in `sdebench-mt/runs/`).
- Host registry: `/home/jc/liveremote/hosts.json` (entry `gh200-dev-probe`) and `/home/jc/liveremote/keys/` (SSH key, known_hosts).
