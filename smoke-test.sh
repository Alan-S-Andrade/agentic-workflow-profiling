#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASK="${*:-Profile this workflow: identify its LLM, tool, control, and data nodes. Do not modify files or external state. Return a concise answer.}"

for workflow in browser-use gpt-researcher autogpt metagpt swe-agent openhands; do
  echo "=== $workflow ==="
  timeout 300 "$ROOT/run.py" "$workflow" "$TASK" || true
done
