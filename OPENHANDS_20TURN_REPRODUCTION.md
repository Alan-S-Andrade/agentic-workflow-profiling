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
The profiler discovers available `perf` events at runtime. Thus cycles,
instructions, generic cache misses, and TLB/L2 counters may differ by CPU;
LLC and memory-bandwidth fields are deliberately `unavailable` unless the x86
host exposes validated PMU events for them.

“Same experiment” means the same workload and collection procedure. It cannot
make scheduler placement, CPU frequency, PMU availability, network latency, or
responses from a hosted model bit-for-bit identical across machines.
