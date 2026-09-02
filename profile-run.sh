#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW="${1:?usage: profile-run.sh WORKFLOW TASK}"
shift
TASK="$*"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$ROOT/traces/${WORKFLOW}-${STAMP}"
PORT="${PROFILE_PORT:-18765}"
mkdir -p "$OUT"

python3 "$ROOT/trace_proxy.py" --port "$PORT" --trace "$OUT/llm.jsonl" >"$OUT/proxy.log" 2>&1 &
PROXY_PID=$!
cleanup() { kill "$PROXY_PID" 2>/dev/null || true; }
trap cleanup EXIT

started="$(date +%s.%N)"
set +e
if [[ "$WORKFLOW" == "autogpt" ]]; then
  printf '%s\n' "$TASK" | PROFILE_OPENAI_BASE_URL="http://127.0.0.1:$PORT/v1" timeout 600 "$ROOT/run.py" "$WORKFLOW" "$TASK" >"$OUT/workflow.log" 2>&1
else
  PROFILE_OPENAI_BASE_URL="http://127.0.0.1:$PORT/v1" timeout 600 "$ROOT/run.py" "$WORKFLOW" "$TASK" >"$OUT/workflow.log" 2>&1
fi
status=$?
set -e
ended="$(date +%s.%N)"
printf '{"workflow":"%s","task":%s,"started_at":%s,"ended_at":%s,"exit_status":%s}\n' \
  "$WORKFLOW" "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$TASK")" "$started" "$ended" "$status" >"$OUT/run.json"
echo "$OUT"
exit "$status"
