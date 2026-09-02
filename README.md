# Agentic workflow profiling

Reproducible launch environment for seven open-source agentic workflows:

- Browser Use
- GPT Researcher
- AutoGPT
- MetaGPT
- SWE-agent
- OpenHands Agent Canvas
- Speculative Tools

Every LLM-backed launcher is configured for a llama.cpp OpenAI-compatible server at `http://localhost:8080/v1`.

## New-machine setup

Requirements: Linux, Git, `uv`, Python build dependencies, and Node.js 22+/npm for OpenHands.

```bash
git clone https://github.com/Alan-S-Andrade/agentic-workflow-profiling.git
cd agentic-workflow-profiling
./setup.sh
llama-server -m /path/to/model.gguf --host 127.0.0.1 --port 8080 --alias local-llama
./smoke-test.sh
```

Upstream repositories are cloned at pinned commits and are intentionally not committed here. `setup.sh` applies the required GPT-5 Nano and unprivileged SWE-agent compatibility patches.

See [RUNNING.md](RUNNING.md) for individual commands and behavior notes.

Profiling output includes per-node local CPU, memory, and elapsed timing. Use `python3 critical_path.py traces/<workflow-run>` to recompute the candidate critical path from `execution.jsonl` and `llm.jsonl`. Use `python3 export_graph.py traces/<workflow-run> --format mermaid` to export a visualization; profiled runs also write `graph.json` and Graphviz `graph.dot`.

## llama.cpp setup

Install `cmake`, a C/C++ toolchain, `curl`, `uv`, and `git`. Download a GGUF model (the example below is about 2 GB):

```bash
mkdir -p .models
uvx --from huggingface_hub hf download \
  bartowski/Llama-3.2-3B-Instruct-GGUF \
  Llama-3.2-3B-Instruct-Q4_K_M.gguf --local-dir .models
./llama-server.sh
```

By default, all seven workflows connect to an existing server on `localhost:8080`; the API key in `.env.example` is only a placeholder required by OpenAI-compatible clients. `run-all-local-llama.sh` profiles every workflow against that server. The optional `llama-server.sh` builds llama.cpp and starts it locally on the same port.
