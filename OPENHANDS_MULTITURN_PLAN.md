# Longer OpenHands multi-turn profiling plan

## Objective

Profile one durable OpenHands `conversation_id` across twenty human turns while
separating human-turn boundaries from the agent's internal LLM and tool loop.
Use `openai/gpt-5.6-luna` through the trace proxy, with a fresh disposable
workspace containing a small target repository for the session. The experiment
tests whether SWE-like code reading, search, editing, and focused test work
have a memory-pressure signature relative to agent planning/orchestration.

## Conversation script

The workload uses many deliberately small SWE-like requests. This makes the
external human turn—not a long internal tool trajectory—the unit of analysis.

1. Inspect a target module and report its public function names.
2. Search for callers of one chosen function; report only the count.
3. Read the focused test file and name its test cases.
4. Run one focused test; report pass/fail.
5. Add one assertion to that test.
6. Rerun the focused test.
7. Search the repository for the relevant error string.
8. Read only the matching implementation function.
9. Add a concise explanatory comment; do not change behavior.
10. Run the focused test again.
11. List the files changed so far.
12. Read the target module's imports and report unused candidates.
13. Run a targeted static check.
14. Correct one explicitly identified formatting issue.
15. Rerun the static check.
16. Search for duplicate logic related to the target function.
17. Read one duplicate occurrence and report whether it differs.
18. Run the focused test once more.
19. Summarize the code change in exactly two sentences.
20. Perform final read-only status and test checks; list artifacts.

Set a small per-turn agent iteration limit (for example 2–3) and submit each
turn only after the prior turn reaches `FinishAction`. The analysis aggregates
all LLM requests, tool spans, CPU, and PSS inside each human turn, then reports
session total, median, p95, and turn-to-turn change. Fine-grained spans remain
available for drill-down but are not the primary comparison unit.

Each message is submitted only after the preceding agent run reaches a
terminal `FinishAction`. The exact prompt, submit timestamp, terminal
timestamp, conversation event IDs, and state snapshot are persisted per turn.

## Required trace artifacts

```text
traces/openhands-multiturn-<timestamp>/
  conversation.json             # session and model identity
  turns.jsonl                   # external human-turn boundaries and events
  llm.jsonl                     # proxy-recorded inference timing
  process-samples.jsonl         # sampled process tree
  process-lanes.json            # per-turn, per-role CPU/PSS aggregates
  hardware-turn-01.json         # per-turn perf counters and availability
  ... hardware-turn-20.json
  timeline.png                  # human-turn timeline and aggregate metrics
  timeline-lanes.png            # same turns with per-process-role lanes
```

## Per-process lanes

Every turn reports the following separately; a missing lane is explicitly
marked `not_observed`, never treated as zero.

| Lane | Processes | CPU metric | PSS metric |
|---|---|---|---|
| Agent server | `agent-server` and its Python runtime | sampled user+system CPU delta | sampled peak PSS |
| Tools | short-lived terminal/file-tool descendants | sampled CPU delta plus exact tool span when instrumented | sampled peak PSS |
| Frontend | Canvas static server and ingress | sampled CPU delta | sampled peak PSS |
| Proxy | `trace_proxy.py` | sampled CPU delta | sampled peak PSS |

Use 10 ms CPU process sampling and 250 ms PSS sampling for the longer run.
`smaps_rollup` is deliberately sampled more slowly because walking mappings at
10 ms perturbs the agent server. Short tools can otherwise be missed between
samples, so the agent-server tool wrapper should also emit an exact start/end
span with PID, CPU, and PSS when possible.

## Per-turn hardware-counter profile

Start `turn_hardware_profile.py` immediately before submitting each user event
and stop it immediately after the matching `FinishAction`. It follows the
agent-server process tree every 10 ms and attaches `perf stat` to every
observed process. The script records cycles, instructions, and generic cache
misses, then derives IPC (`instructions / cycles`) for the turn and each
observed role. The hardware JSON is joined to `turn_id` in the timeline.

LLC misses and memory bandwidth are required measurements, but never inferred
from generic cache misses or PSS. They are enabled only when `perf list` and a
counter probe expose a usable LLC event and memory-controller/DRAM event. The
current aarch64 host exposes generic `cycles`, `instructions`, and
`cache-misses`, but no LLC-specific or memory-bandwidth PMU event, so those
columns must be labelled `unavailable` on this host. A host exposing those PMU
events is required to test the memory-bound hypothesis directly.

## Why tool CPU/PSS was not observed in the baseline

The 100 ms sampler walks `/proc`, discovers descendant PIDs, and records their
next sample. Many terminal actions in the baseline completed faster than that
interval, so a tool process could start and exit entirely between two walks.
The sampler therefore never saw a PID to attribute CPU or PSS to. In addition,
some OpenHands terminal work is mediated by the already-running agent-server,
so process-tree sampling alone cannot always split the tool's resource use from
the server's own work.

Reducing the interval to 10 ms improves capture but does not make it exact and
adds profiler overhead. Exact attribution needs instrumentation at the
agent-server terminal/file-tool boundary: record the child PID at launch, read
its resource counters at exit, and emit a tool span linked to the current
`conversation_id`, `turn_id`, and action event. The timeline will then show
tool lanes as exact spans, while retaining sampled values for the longer-lived
agent-server, frontend, and proxy processes.

## Metric definitions and limits

- **Sampled process-tree CPU**: sum of each observed process's user+system CPU
  delta during a turn. It is accurate to the sample boundaries, but may
  undercount short-lived processes or CPU immediately outside those boundaries.
- **Sampled aggregate PSS**: the maximum, at any sample, of the sum of PSS
  from `/proc/<pid>/smaps_rollup` for observed processes. Shared pages are
  apportioned among mappers, so PSS is the primary comparative physical-memory
  estimate; a process can still be missed between samples.
- **CPU utilization**: `sampled_process_tree_cpu_ms / turn_duration_ms * 100`;
  it is percentage of one logical CPU and can exceed 100% when local processes
  run in parallel.
- Remote model-server CPU/PSS is outside this host trace and is reported as
  unavailable rather than folded into local metrics.

## Current three-turn baseline

The completed OpenHands baseline in
`traces/openhands-multiturn-20260915T154000Z/` measured:

| Turn | Duration | LLM calls | Sampled process-tree CPU | CPU utilization |
|---|---:|---:|---:|---:|
| Create plan | 10.70 s | 3 | 560 ms | 5.23% |
| Amend plan | 11.37 s | 3 | 660 ms | 5.80% |
| Inspect/finalize | 9.32 s | 2 | 450 ms | 4.83% |

The agent-server, frontend, and proxy were observed by the current sampler.
Tool processes were not independently observed at its 100 ms cadence; the
longer run therefore uses the 10 ms cadence plus tool-span instrumentation.
