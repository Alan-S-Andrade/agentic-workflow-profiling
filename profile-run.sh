#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW="${1:?usage: profile-run.sh WORKFLOW TASK}"
shift
TASK="$*"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$ROOT/traces/${WORKFLOW}-${STAMP}"
PORT="${PROFILE_PORT:-18765}"
RUN_TIMEOUT="${WORKFLOW_RUN_TIMEOUT:-600}"
mkdir -p "$OUT"
: >"$OUT/execution.jsonl"

TRACE_PROXY_UPSTREAM="${PROFILE_PROXY_UPSTREAM:-http://localhost:8080}" \
python3 "$ROOT/trace_proxy.py" --port "$PORT" --trace "$OUT/llm.jsonl" >"$OUT/proxy.log" 2>&1 &
PROXY_PID=$!
cleanup() { kill "$PROXY_PID" 2>/dev/null || true; }
trap cleanup EXIT

started="$(date +%s.%N)"
set +e
if [[ "$WORKFLOW" == "autogpt" ]]; then
  # AutoGPT asks for its legal acknowledgement before the task in TTY mode.
  printf -v QUOTED_TASK '%q' "$TASK"
  { printf 'y\n%s\n' "$TASK"; yes y; } | script -qec "env PROFILE_OPENAI_BASE_URL=http://127.0.0.1:$PORT/v1 PROFILE_NODE_TRACE=$OUT/execution.jsonl timeout --kill-after=10 $RUN_TIMEOUT $ROOT/run.py $WORKFLOW $QUOTED_TASK" /dev/null >"$OUT/workflow.log" 2>&1
elif [[ "$WORKFLOW" == "openhands" ]]; then
  PROFILE_OPENAI_BASE_URL="http://127.0.0.1:$PORT/v1" PROFILE_NODE_TRACE="$OUT/execution.jsonl" OPENHANDS_START_ONLY=1 timeout --kill-after=10 "$RUN_TIMEOUT" "$ROOT/run.py" "$WORKFLOW" "$TASK" >"$OUT/workflow.log" 2>&1
else
  PROFILE_OPENAI_BASE_URL="http://127.0.0.1:$PORT/v1" PROFILE_NODE_TRACE="$OUT/execution.jsonl" timeout --kill-after=10 "$RUN_TIMEOUT" "$ROOT/run.py" "$WORKFLOW" "$TASK" >"$OUT/workflow.log" 2>&1
fi
status=$?
set -e
ended="$(date +%s.%N)"
printf '{"workflow":"%s","task":%s,"started_at":%s,"ended_at":%s,"exit_status":%s}\n' \
  "$WORKFLOW" "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$TASK")" "$started" "$ended" "$status" >"$OUT/run.json"
python3 "$ROOT/critical_path.py" "$OUT" >"$OUT/critical-path.json"
python3 "$ROOT/export_graph.py" "$OUT" --format json >"$OUT/graph.json"
python3 "$ROOT/export_graph.py" "$OUT" --format dot >"$OUT/graph.dot"
echo "$OUT"
exit "$status"
