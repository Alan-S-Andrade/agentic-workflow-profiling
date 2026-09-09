# Running the workflows

Start llama.cpp on `localhost:8080` with the model alias `local-llama`, then run:

```sh
./run.py browser-use "Open example.com and return its title"
./run.py gpt-researcher "Research a small topic"
./run.py speculative-tools "What's the weather in London and the forecast?"
./run.py metagpt "Create a command-line calculator"
./run.py swe-agent "Fix the failing test"
./run.py autogpt "Complete a small task"
./run.py openhands "start"
```

For a profiled GPT Researcher run using an API key exported by `~/.bashrc`:

```sh
bash run-gpt-researcher-openai.txt
```

The runner records the concurrent subquery and per-URL scrape spans in addition
to planning, web-search coordination, and report generation.

All workflows default to `http://localhost:8080/v1`, use the model name `local-llama`, and supply a non-secret placeholder API key. Override `PROFILE_OPENAI_BASE_URL`, `WORKFLOW_MODEL`, or `OPENAI_API_KEY` in `.env` when your llama.cpp configuration differs. GPT Researcher uses DuckDuckGo and local embeddings to avoid additional API services.

AutoGPT is interactive and requests task/tool approval. OpenHands starts Agent Canvas at `http://localhost:8000`; its task is submitted through the UI. The smoke script limits each launcher to five minutes.

Speculative Tools runs its OpenAI-compatible agent adapter with the upstream demo's safe weather tools. It accepts one task, allows up to five model iterations, and reports tool calls and speculation hits/misses.

SWE-agent uses `swe-smoke-target/`, generated locally by the runner, so smoke tests do not modify the framework checkout.

## SWE-agent: isolated target and process lineage

SWE-agent's selected `local` deployment runs commands on this host and resets
its target Git working tree at startup. Never point it at a working tree with
edits you want to keep: the runner now refuses dirty targets.

For the go-redis task, create a disposable clone and run the fully instrumented
launcher:

```bash
target="$(./prepare-swe-go-redis-target.sh)"
bash run-swe-agent-go-redis-openai.txt "$target"
```

This enables `PROFILE_PROCESS_TRACE=1`. In addition to `execution.jsonl` and
`llm.jsonl`, the trace directory will contain:

- `environment.json`: declared deployment mode plus host cgroup, namespace,
  container-marker, and virtualization evidence;
- `process.strace.*`: raw `fork`/`clone`/`vfork`, `execve`, `setns`, and
  `unshare` observations, one file per traced PID;
- `process-events.jsonl`: normalized spawn, exec, and namespace-boundary
  events;
- `process-samples.jsonl`: sampled per-process CPU, RSS, cgroup, and namespace
  metadata while each observed descendant is alive;
- `process-graph.dot` and `process-graph.png`: the observed process lineage.

`strace` follows host processes; it adds material overhead and is therefore
off by default. A `docker`/`podman` exec or namespace/cgroup boundary is
evidence of container work. Host virtualization evidence does not by itself
prove that an individual workflow step ran in a VM.

Run `./run-all-local-llama.sh` to profile all seven workflows through the already-running llama.cpp server. It checks the server health first and records each workflow under `traces/`. Each trace directory contains `execution.jsonl` with local agent/tool/control spans, `llm.jsonl` with LLM spans, `critical-path.json` with a candidate wall-clock critical path, and `graph.json`/`graph.dot` with the recorded and inferred workflow graph. Convert the DOT file with Graphviz, or generate Mermaid with `python3 export_graph.py traces/<run> --format mermaid`. Execution spans include elapsed time, process CPU, current/start/end/peak RSS, Python `tracemalloc` allocation state, and page-fault/context-switch counters. RSS is for the traced process; browser, llama.cpp, and other child-process memory must be profiled as separate processes.
