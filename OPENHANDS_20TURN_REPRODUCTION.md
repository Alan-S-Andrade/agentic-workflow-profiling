# Reproduce the 20-turn OpenHands profiling experiment

This launcher runs the same twenty human prompts, OpenHands settings, recovery
policy, remote model configuration, target repository revision, and local
profiling regime as the robust reference run.

```bash
export OPENAI_API_KEY='...'
bash run_openhands_20turn_reproducible.sh
```

The target is pinned to go-redis commit
`8010edc761c98482aa804ad3d3c8447a09528715`. The script rejects another
revision so code, tests, and prompt results remain comparable. It uses
`openhands-agent-server==1.44.0`, `openai/gpt-5.6-luna`, temperature zero,
terminal-only tools, six agent iterations per human turn, 120-second turn
timeout, and at most two recovery attempts per logical turn.

Override paths or ports only when necessary:

```bash
OPENHANDS_SOURCE_WORKSPACE=/path/to/go-redis \
OPENHANDS_TRACE_DIR=/path/to/output \
OPENHANDS_AGENT_SERVER_COMMAND='uvx --from openhands-agent-server==1.44.0 agent-server' \
bash run_openhands_20turn_reproducible.sh
```

The output contains `turns.jsonl`, LLM proxy spans, process CPU/PSS/core
samples, per-turn hardware-counter JSON, recovery edges, and `timeline.png`.
It also contains `agent-events.jsonl`, an append-only copy of every event
returned by the agent server. Together with `llm.jsonl`, this is a complete
step-level capture source for the no-LLM replay helper.
The profiler discovers available `perf` events at runtime. Thus cycles,
instructions, generic cache misses, and TLB/L2 counters may differ by CPU;
LLC and memory-bandwidth fields are deliberately `unavailable` unless the x86
host exposes validated PMU events for them.

“Same experiment” means the same workload and collection procedure. It cannot
make scheduler placement, CPU frequency, PMU availability, network latency, or
responses from a hosted model bit-for-bit identical across machines.

## Timed no-LLM Docker replay

After a successful capture, copy the pinned source checkout to a fresh durable
workspace and replay the recorded terminal actions while replacing each
recorded inference with a wait of its measured duration:

```bash
sudo docker build -f Dockerfile.openhands-replay -t openhands-trace-replay:go1.24 .
cp -a swe-default-target /tmp/openhands-replay-workspace
python3 replay_openhands_trace.py traces/openhands-multiturn-robust-<timestamp> \
  --workspace /tmp/openhands-replay-workspace
```

The replay writes one `steps.jsonl` record for every terminal action and LLM
wait. In Docker mode, it starts exactly one network-disabled persistent sandbox
per replay session, bind-mounts that session's workspace at `/workspace`, and
runs every terminal action through `docker exec` in the same sandbox. The
sandbox remains alive during each mocked inference wait, so its process tree,
container overlay, environment, and mapped pages model a Coder/Terminal
session rather than an ephemeral action container. `--idle-hold-seconds` can
retain completed sessions for a controlled idle-residency interval.

For a multi-session capacity run (PSS sampled at 100 ms; CPU at 10 ms):

```bash
sudo python3 colocate_openhands_docker_replays.py traces/openhands-multiturn-robust-<timestamp> \
  --source-workspace swe-default-target --output traces/persistent-session-run \
  --concurrency 48 --idle-hold-seconds 600
```

For this host's node-0-only NUMA experiment, bind the collector/controller and
each sandbox explicitly. Node 0 is CPUs `0,2,...,46` and memory node `0`:

```bash
numactl --cpunodebind=0 --membind=0 sudo python3 colocate_openhands_docker_replays.py \
  traces/openhands-multiturn-robust-<timestamp> --source-workspace swe-default-target \
  --output traces/node0-session-run --concurrency 24 --cpuset-cpus=0,2,4,6,8,10,12,14,16,18,20,22,24,26,28,30,32,34,36,38,40,42,44,46 \
  --cpuset-mems=0 --idle-hold-seconds 600
```

Its PSS fields cover only labelled sandbox processes. `replay_shared_clean_bytes`
and `replay_shared_dirty_bytes` expose shared mappings, but are intentionally
not added to PSS, which already apportions shared resident pages. This remains
a no-LLM execution model: it preserves observed tool commands and inference
wall-time but does not recreate agent reasoning or tool-result-conditioned
branching.

## Active node-0 replay capacity

To measure CPU while sessions are actively replaying, loop each captured trace
with its recorded LLM waits. Containers stay on node-0 CPUs and node-0 memory;
the run stops at 90% of node-0 DRAM and writes samples plus a Matplotlib plot:

```bash
sudo python3 run_node0_trace_replay_capacity.py \
  traces/openhands-multiturn-capture-<timestamp> \
  --source-workspace swe-default-target \
  --output traces/node0-active-capacity \
  --active-replay --replay-speed 1 --sample-window-seconds 5
```

`--replay-speed 1` preserves captured inference waits without making an LLM
request. The x-axis is resident sessions, the left axis is aggregate Docker
cgroup memory, and the right axis is node-0 CPU.

## Local versus remote NUMA turn latency

Compare complete captured turns with the sandbox restricted to node-0 memory
versus node-1 memory while both cases execute on node-0 CPUs:

```bash
sudo python3 numa_trace_turn_latency.py \
  traces/openhands-multiturn-capture-<timestamp> \
  --source-workspace swe-default-target \
  --output traces/numa-turn-latency --repeats 3 --replay-speed 1
```

The output separates terminal execution time from mocked LLM-wait time and
writes `numa-trace-turn-terminal-latency.png` and `summary.json`.
