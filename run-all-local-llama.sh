#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${LLAMA_PORT:-18080}"
TASK="${*:-Profile this workflow: identify its LLM, tool, control, and data nodes. Do not modify files or external state. Return a concise answer.}"

"$ROOT/llama-server.sh" >"$ROOT/llama-server.log" 2>&1 &
SERVER_PID=$!
cleanup() { kill "$SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT

for i in $(seq 1 120); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/v1/models" >/dev/null

index=0
for workflow in browser-use gpt-researcher autogpt metagpt swe-agent openhands; do
  echo "=== $workflow (local llama.cpp) ==="
  PROFILE_PROXY_UPSTREAM="http://127.0.0.1:$PORT" \
  PROFILE_PORT="$((PORT + 1 + index))" \
  OPENAI_API_KEY="sk-local-llama" \
  WORKFLOW_MODEL="local-llama" \
  timeout "${WORKFLOW_TIMEOUT:-600}" "$ROOT/profile-run.sh" "$workflow" "$TASK" || true
  index=$((index + 1))
done
