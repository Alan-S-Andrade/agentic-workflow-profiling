# Agentic workflow profiling

Reproducible launch environment for six open-source agentic workflows:

- Browser Use
- GPT Researcher
- AutoGPT
- MetaGPT
- SWE-agent
- OpenHands Agent Canvas

Every LLM-backed launcher is pinned to `gpt-5-nano`, the least-expensive model selected for this project.

## New-machine setup

Requirements: Linux, Git, `uv`, Python build dependencies, and Node.js 22+/npm for OpenHands.

```bash
git clone https://github.com/Alan-S-Andrade/agentic-workflow-profiling.git
cd agentic-workflow-profiling
./setup.sh
printf 'OPENAI_API_KEY=%s\n' 'YOUR_PLATFORM_KEY' > .env
chmod 600 .env
./smoke-test.sh
```

Upstream repositories are cloned at pinned commits and are intentionally not committed here. `setup.sh` applies the required GPT-5 Nano and unprivileged SWE-agent compatibility patches.

See [RUNNING.md](RUNNING.md) for individual commands and behavior notes.

## Fully local llama.cpp mode

Install `cmake`, a C/C++ toolchain, `curl`, `uv`, and `git`. Download a GGUF model (the example below is about 2 GB):

```bash
mkdir -p .models
uvx --from huggingface_hub hf download \
  bartowski/Llama-3.2-3B-Instruct-GGUF \
  Llama-3.2-3B-Instruct-Q4_K_M.gguf --local-dir .models
./run-all-local-llama.sh
```

`llama-server.sh` builds `llama.cpp` under `.tools/llama.cpp` if needed, serves `local-llama` on `127.0.0.1:18080`, and the six workflows receive no hosted-provider endpoint or API credential. Override `LLAMA_MODEL`, `LLAMA_PORT`, `LLAMA_CTX_SIZE`, or `LLAMA_BUILD_JOBS` as needed.
