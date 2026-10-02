# Bounded SWE-bench Verified microVM sweep

This is an addition to, not a replacement for, the recorded trace-replay
experiment.  The existing replay remains the controlled workload baseline.
This sweep measures colocation of short-lived autonomous replay sessions.

## Session model

One session owns one Cloud Hypervisor microVM and one distinct SWE-bench
Verified instance. The VM has its private official instance filesystem and a
saved mini-SWE-agent trajectory. It sleeps for each saved LLM inference span,
executes every saved tool command locally, then powers off. There is no replay
loop, idle hold, live model call, or host-to-guest command channel.

The experiment uses the existing terminology exactly:

| Term | Meaning |
| --- | --- |
| session | One VM sandbox running one benchmark instance |
| turn | One mini-SWE-agent model response |
| step | One Bash command emitted in a turn |

A turn can contain more than one step.  The runner must write one step record
per executed command and one turn record after all of that turn's steps have
completed.  It must retain the existing continuous workload samples:
`resident_sessions`, aggregate cgroup memory, aggregate PSS, and Node-0 CPU.

## Admission schedule

Admission is deliberately batched to create a rising colocation load:

* Admit 10 new sessions concurrently.
* Start the next batch 20 seconds after the preceding batch's scheduled start.
* Stop further admissions when the existing memory or CPU stop threshold is
  reached.
* Do not replace completed sessions.  This makes each scheduled benchmark
  instance short-lived rather than creating an indefinitely sustained load.

For all 500 Verified instances, this produces 50 admission batches with the
last batch scheduled at 980 seconds.  A capacity-limited run can stop earlier.

## Reproducible run

The checked-in `/yig` trajectories are the workload corpus. Create a plan or
run the sweep directly:

```bash
python3 run_yig_microvm_replay.py \
  --output traces/yig-swebench-verified-microvm

# After rebuilding microvm-assets/replay-rootfs.img and confirming /dev/kvm:
python3 run_yig_microvm_replay.py \
  --output traces/yig-swebench-verified-microvm \
  --run
```

The default plan contains every distinct `/yig` trajectory (currently 100),
with batches of 10 at 20-second intervals. Use `--sessions` for a staging run;
its manifest records the selected subset.

## Environment boundary

The base replay rootfs is offline and supplies the local replay process. Each
session's second, private disk is built from that instance's official
SWE-bench OCI image with `/replay/manifest.json` installed. The guest chroots
into this disk for tool execution. The host only creates disks, starts VMs,
and samples their cgroups; it never sends a command after launch.

Each VM sees the full configured Node-0 CPU set and has no per-session CPU
quota, so runnable guest work shares host cycles naturally. It also has no
per-session host memory limit: the guest memory ceiling defaults to all Node-0
RAM, while resident pages are allocated on demand. The global admission
thresholds bound aggregate Node-0 pressure.
