# Updated workflow DAGs and resource metrics

Metrics below come from the profiled runs in `traces/`. `n/a` means the node
was either in the separate llama.cpp proxy process (which records duration only)
or was terminated before its node-end sample was written.

Legend: `L` = LLM, `T` = tool, `C` = control, `D` = data.

## Browser Use

```text
D task -> C agent loop -> L llama.cpp -> T Playwright -> D page title -> C -> D answer
```

| Node | Type | CPU ms | Duration ms | Peak RSS |
|---|---|---:|---:|---:|
| agent loop | C | n/a | n/a (process ended before sample) | n/a |
| chat completion 1 | L | n/a | 25,422.735 | n/a |
| chat completion 2 | L | n/a | 33,781.404 | n/a |
| Playwright/page title | T | n/a | n/a | n/a |

## GPT Researcher

```text
D question -> C research planner -> T DuckDuckGo/scraper -> D sources -> L llama.cpp
  -> C report writer -> D report
```

| Node | Type | CPU ms | Duration ms | Peak RSS |
|---|---|---:|---:|---:|
| research planner | C | n/a | n/a (timeout) | n/a |
| chat completion 1 | L | n/a | 28,082.083 | n/a |
| chat completion 2 | L | n/a | 31,515.319 | n/a |
| web scraping | T | n/a | n/a | n/a |
| report writer | C | n/a | n/a (timeout) | n/a |

## Speculative Tools

```text
D task -> L llama.cpp -> C adapter loop -> C prediction/cache -> T weather tools
  -> D cached/result data -> C -> D response + stats
```

| Node | Type | CPU ms | Duration ms | Peak RSS |
|---|---|---:|---:|---:|
| agent loop | C | 381.800 | 97,574.250 | n/a |
| chat completion 1 | L | n/a | 52,875.281 | n/a |
| get_weather | T | 0.435 | 300.717 | n/a |
| get_forecast | T | 0.290 | 250.527 | n/a |
| chat completion 2 | L | n/a | 43,766.200 | n/a |

## AutoGPT

```text
D objective -> C AutoGPT loop -> L llama.cpp -> C permission/action router
  -> T agent action -> D observation -> C -> D result
```

| Node | Type | CPU ms | Duration ms | Peak RSS |
|---|---|---:|---:|---:|
| AutoGPT workflow | C | 9,138.572 | 176,706.957 | n/a |
| model discovery (3 requests) | L | n/a | 6.522 total | n/a |
| chat completion 1 | L | n/a | 141,772.252 | n/a |
| chat completion 2 | L | n/a | 32,735.977 | n/a |
| permission/action | T/C | n/a | n/a (interactive stop) | n/a |

## MetaGPT

```text
D project idea -> C role/team orchestration -> L llama.cpp -> T file/code generation
  -> D project files -> C -> D design summary
```

| Node | Type | CPU ms | Duration ms | Peak RSS |
|---|---|---:|---:|---:|
| MetaGPT workflow | C | 18,248.928 | 157,979.790 | n/a |
| chat completion 1 | L | n/a | 52,521.434 | n/a |
| chat completion 2 | L | n/a | 101,283.380 | n/a |
| role/file generation | T/C | n/a | included above | n/a |

## SWE-agent

```text
D repository/problem -> C trajectory loop -> L llama.cpp -> C thought/action parser
  -> T editor/shell -> D files/diff/output -> C -> D patch/summary
```

| Node | Type | CPU ms | Duration ms | Peak RSS |
|---|---|---:|---:|---:|
| SWE-agent workflow | C | 9,980.284 | 4,316.648 | n/a |
| tool bundle setup | T | n/a | failed/partial | n/a |
| model request | L | n/a | n/a (connection retry) | n/a |

## OpenHands Agent Canvas

```text
D UI task -> C Agent Canvas session -> L llama.cpp -> C agent-server router
  -> T workspace/browser/shell -> D observations/artifacts -> C -> D UI response
```

| Node | Type | CPU ms | Duration ms | Peak RSS |
|---|---|---:|---:|---:|
| Agent Canvas startup | C | n/a | n/a (long-running service) | n/a |
| agent-server LLM call | L | n/a | n/a (task submitted in UI) | n/a |
| UI/workspace tools | T | n/a | n/a | n/a |

## Reproduce with fresh metrics

```bash
WORKFLOW_RUN_TIMEOUT=600 ./profile-run.sh WORKFLOW "TASK"
python3 export_graph.py traces/WORKFLOW-TIMESTAMP --format mermaid
cat traces/WORKFLOW-TIMESTAMP/critical-path.json
```

The JSON graph files retain the raw per-node CPU, duration, and RSS fields when
the workflow exits cleanly.

## SWE-agent per-command resource metrics

`setup.sh` also applies `swe-agent-step-resource-profiling.patch`.  Each
SWE-agent `SWEEnv.communicate()` call now writes a `tool` node named
`swe-agent.shell` to `execution.jsonl`, including the submitted shell command,
elapsed duration, `cpu_ms`, `cpu_utilization_pct`, and resident-set start,
end, delta, and sampled peak (`rss_start_bytes`, `rss_end_bytes`,
`rss_delta_bytes`, and `peak_rss_bytes`).  CPU utilization is process CPU time
divided by elapsed time, as a percent of one logical CPU; it can exceed 100%
when waited-for local child processes run in parallel.

RSS is a resident-memory measurement, not a strict operating-system working
set estimate.  It is sampled for the SWE-agent process that drives the local
runtime; `children_peak_rss_bytes` remains the OS high-water value for reaped
children.  For exact command-process working-set attribution in a container or
remote deployment, profile that runtime with cgroups/eBPF as a separate layer.
