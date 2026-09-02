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
