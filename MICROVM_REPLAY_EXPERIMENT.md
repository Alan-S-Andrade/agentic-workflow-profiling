# Node-0 persistent-microVM trace replay

This replaces the persistent Docker sandbox capacity experiment.  A tenant
session owns exactly one long-lived Cloud Hypervisor microVM for its full
workflow.  The host mocks each recorded LLM span by sleeping for its saved
duration; every recorded non-LLM shell command is sent to that session's guest
agent.  No model endpoint or API key is used during replay.

## Workload mix

`microvm_trace_replay_plan.py` round-robins one session per new tenant across:

1. go-redis (`mini-swe-20turn-*`)
2. GORM (`external-gorm-20turn-*`)
3. Gin (`external-gin-20turn-*`)
4. Cobra (`external-cobra-20turn-*`)
5. Testify (`external-testify-20turn-*`)

Each source has 20 semantic turns and retains its own inference durations,
commands, exit codes, and tool timings.

## Required guest image contract

Provide a Cloud Hypervisor-compatible kernel and writable rootfs containing Go,
git, bash, and the SWE-agent tool registry.  Its init must start
`/usr/local/bin/replay-agent`, a vsock service at port `52`, accepting one JSON
line per request:

```json
{"op":"exec","cwd":"/workspace","command":"go test ./..."}
```

It returns one JSON line with `exit_status`, `stdout`, and `stderr`.  The rootfs
is private to each VM, so edited files, package/build caches, and processes
remain session-local across all 20 turns.

## NUMA boundary

The VMM process is launched with `taskset` over Node-0 CPUs and
`numactl --membind=0`.  Consequently its vCPU threads and allocated guest RAM
are restricted to Node 0.  The runner samples the VMM cgroup plus guest-memory
allocation and stops admission before 90% of Node-0 DRAM.

## Run

Cloud Hypervisor v53.0 and `ch-remote` are installed at `/usr/local/bin` on
this host.  The host must additionally expose `/dev/kvm`; this environment
currently does not, so installation alone cannot start a hardware-accelerated
microVM.  No microVM is launched by default.  After supplying the guest assets
and agent, source `cloud-hypervisor.env`, review the generated plan, and run:

```bash
sudo python3 microvm_trace_replay_plan.py --run \
  --kernel /path/to/vmlinux --rootfs /path/to/replay-rootfs.img \
  --output traces/node0-microvm-replay-YYYYMMDDTHHMMSSZ
```

The `--run` path intentionally validates `cloud-hypervisor`, `numactl`, the
assets, and the guest-agent transport before admitting any tenant VM.
