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

snapshot_args=(--output "$OUT/environment.json" --workflow "$WORKFLOW")
if [[ "$WORKFLOW" == "swe-agent" ]]; then
  # The selected SWE configuration uses SWE-ReX LocalDeployment.  Recording
  # this makes it explicit that agent commands run on the host, not in a
  # framework-created container or VM.
  snapshot_args+=(--deployment local)
fi
python3 "$ROOT/environment_snapshot.py" "${snapshot_args[@]}"

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
  status=$?
elif [[ "$WORKFLOW" == "openhands" ]]; then
  PROFILE_OPENAI_BASE_URL="http://127.0.0.1:$PORT/v1" PROFILE_NODE_TRACE="$OUT/execution.jsonl" OPENHANDS_START_ONLY=1 timeout --kill-after=10 "$RUN_TIMEOUT" "$ROOT/run.py" "$WORKFLOW" "$TASK" >"$OUT/workflow.log" 2>&1
  status=$?
elif [[ "$WORKFLOW" == "swe-agent" && "${PROFILE_PROCESS_TRACE:-0}" == "1" ]]; then
  if ! command -v strace >/dev/null; then
    echo "PROFILE_PROCESS_TRACE=1 requires strace" >"$OUT/workflow.log"
    status=127
  else
    # -ff follows fork/clone descendants and writes one file per PID.  The
    # sampler complements it with sampled per-process CPU/RSS/cgroup/namespace
    # metadata while processes are alive.
    PROFILE_OPENAI_BASE_URL="http://127.0.0.1:$PORT/v1" PROFILE_NODE_TRACE="$OUT/execution.jsonl" \
      strace -ff -ttt -T -s 512 -o "$OUT/process.strace" -e trace=process,execve,setns,unshare \
      timeout --kill-after=10 "$RUN_TIMEOUT" "$ROOT/run.py" "$WORKFLOW" "$TASK" >"$OUT/workflow.log" 2>&1 &
    TRACE_PID=$!
    python3 "$ROOT/process_sampler.py" --root-pid "$TRACE_PID" --output "$OUT/process-samples.jsonl" &
    SAMPLER_PID=$!
    wait "$TRACE_PID"
    status=$?
    kill "$SAMPLER_PID" 2>/dev/null || true
    wait "$SAMPLER_PID" 2>/dev/null || true
  fi
else
  PROFILE_OPENAI_BASE_URL="http://127.0.0.1:$PORT/v1" PROFILE_NODE_TRACE="$OUT/execution.jsonl" timeout --kill-after=10 "$RUN_TIMEOUT" "$ROOT/run.py" "$WORKFLOW" "$TASK" >"$OUT/workflow.log" 2>&1
  status=$?
fi
set -e
ended="$(date +%s.%N)"
printf '{"workflow":"%s","task":%s,"started_at":%s,"ended_at":%s,"exit_status":%s}\n' \
  "$WORKFLOW" "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$TASK")" "$started" "$ended" "$status" >"$OUT/run.json"
python3 "$ROOT/critical_path.py" "$OUT" >"$OUT/critical-path.json"
python3 "$ROOT/export_graph.py" "$OUT" --format json >"$OUT/graph.json"
python3 "$ROOT/export_graph.py" "$OUT" --format dot >"$OUT/graph.dot"
if [[ "${PROFILE_PROCESS_TRACE:-0}" == "1" ]] && compgen -G "$OUT/process.strace.*" >/dev/null; then
  python3 "$ROOT/process_graph.py" --input-prefix "$OUT/process.strace" --events "$OUT/process-events.jsonl" --dot "$OUT/process-graph.dot"
  if command -v dot >/dev/null; then
    dot -Tpng "$OUT/process-graph.dot" -o "$OUT/process-graph.png"
  fi
fi
echo "$OUT"
exit "$status"
