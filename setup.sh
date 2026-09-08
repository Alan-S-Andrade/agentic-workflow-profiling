#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

clone_at() {
  local directory="$1" url="$2" revision="$3"
  if [[ ! -d "$directory/.git" ]]; then
    git clone --filter=blob:none "$url" "$directory"
  fi
  git -C "$directory" fetch --depth 1 origin "$revision"
  git -C "$directory" checkout --detach "$revision"
}

clone_at browser-use https://github.com/browser-use/browser-use.git 564007d3d64cda0cccd29f37d70f8cfe9bebcd62
clone_at gpt-researcher https://github.com/assafelovic/gpt-researcher.git 6f998577d547b1e54ec662dac63583aa11e3b84b
clone_at autogpt https://github.com/Significant-Gravitas/AutoGPT.git 32a43d005c0c42079ceba68d9a49c28e0eeaa6c7
clone_at metagpt https://github.com/FoundationAgents/MetaGPT.git 11cdf466d042aece04fc6cfd13b28e1a70341b1f
clone_at swe-agent https://github.com/SWE-agent/SWE-agent.git 3ea751c087f32b16e039a2233dd6eefecef325d5
clone_at openhands https://github.com/OpenHands/OpenHands.git b4428e1f8529fe726039437c8e54a7e7319986eb
clone_at speculative-tools https://github.com/joelvarun/speculative-tools.git c93cad9e6449be5e3953ef563943c28b3a962629

apply_once() {
  local directory="$1" patch="$2"
  if git -C "$directory" apply --reverse --check "$patch" 2>/dev/null; then
    return
  fi
  git -C "$directory" apply --check "$patch"
  git -C "$directory" apply "$patch"
}

apply_once metagpt "$ROOT/patches/metagpt-gpt5.patch"
apply_once swe-agent "$ROOT/patches/swe-agent-local-gpt5.patch"
apply_once swe-agent "$ROOT/patches/swe-agent-step-resource-profiling.patch"

uv sync --directory browser-use
uv run --directory browser-use playwright install chromium
uv venv --python 3.11 gpt-researcher/.venv
uv pip install --python gpt-researcher/.venv/bin/python -e gpt-researcher ddgs langchain-huggingface langchain-mcp-adapters sentence-transformers
uv sync --directory autogpt/classic
uv pip install --python autogpt/classic/.venv/bin/python -e autogpt/classic
uv venv --python 3.10 metagpt/.venv
uv pip install --python metagpt/.venv/bin/python -e metagpt
uv pip install --python metagpt/.venv/bin/python 'click<8.2'
uv sync --directory swe-agent
uv venv --python 3.11 speculative-tools/.venv
uv pip install --python speculative-tools/.venv/bin/python -e 'speculative-tools[openai]'

mkdir -p .tools/openhands /tmp/swe-agent-home /tmp/swe-agent-tools
NODE_BIN="$ROOT/.tools/node-v22.22.0-linux-x64/bin"
if [[ ! -x "$NODE_BIN/node" ]] && { ! command -v node >/dev/null || [[ "$(node -p 'process.versions.node.split(".")[0]')" -lt 22 ]]; }; then
  command -v curl >/dev/null || { echo "curl is required to install Node.js 22" >&2; exit 1; }
  command -v tar >/dev/null || { echo "tar is required to install Node.js 22" >&2; exit 1; }
  node_archive="/tmp/node-v22.22.0-linux-x64.tar.xz"
  curl -fsSL https://nodejs.org/dist/v22.22.0/node-v22.22.0-linux-x64.tar.xz -o "$node_archive"
  tar -xJf "$node_archive" -C "$ROOT/.tools"
fi
if [[ -x "$NODE_BIN/node" ]]; then
  export PATH="$NODE_BIN:$PATH"
fi
if command -v npm >/dev/null; then
  npm install --prefix .tools/openhands @openhands/agent-canvas@1.16.0
else
  echo "npm was not found; install Node.js 22+, then run:" >&2
  echo "  npm install --prefix .tools/openhands @openhands/agent-canvas@1.16.0" >&2
fi

if [[ ! -d swe-smoke-target/.git ]]; then
  mkdir -p swe-smoke-target
  printf '%s\n' '# SWE-agent smoke target' '' 'Read-only target used to exercise the profiling workflow.' > swe-smoke-target/README.md
  git -C swe-smoke-target init -q
  git -C swe-smoke-target add README.md
  git -C swe-smoke-target -c user.name=smoke -c user.email=smoke@localhost commit -qm init
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi

echo "Setup complete. Start llama.cpp on localhost:8080, then run ./smoke-test.sh"
