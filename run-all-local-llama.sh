#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${LLAMA_PORT:-8080}"
TASK="${*:-Profile this workflow: identify its LLM, tool, control, and data nodes. Do not modify files or external state. Return a concise answer.}"

curl -fsS "http://localhost:$PORT/health" >/dev/null || {
  echo "llama.cpp is not reachable at http://localhost:$PORT" >&2
  exit 1
}

index=0
for workflow in browser-use gpt-researcher speculative-tools autogpt metagpt swe-agent openhands; do
  echo "=== $workflow (local llama.cpp) ==="
  PROFILE_PROXY_UPSTREAM="http://localhost:$PORT" \
  PROFILE_PORT="$((PORT + 1 + index))" \
  WORKFLOW_RUN_TIMEOUT="${WORKFLOW_TIMEOUT:-600}" \
  OPENAI_API_KEY="sk-local-llama" \
  WORKFLOW_MODEL="local-llama" \
  timeout "${WORKFLOW_TIMEOUT:-600}" "$ROOT/profile-run.sh" "$workflow" "$TASK" || true
  index=$((index + 1))
done
