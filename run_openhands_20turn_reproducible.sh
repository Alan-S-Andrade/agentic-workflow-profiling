#!/usr/bin/env bash
# Reproduce the 20-turn OpenHands profiling experiment on another Linux host.
#
# Required: bash, curl, git, Python 3, uv, and OPENAI_API_KEY.
# ``perf`` is optional; the trace remains complete without hardware counters.
# Optional: set OPENHANDS_AGENT_SERVER_COMMAND to a locally pinned agent-server
# command if the host already has one; the default pins the package version.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_WORKSPACE="${OPENHANDS_SOURCE_WORKSPACE:-$ROOT/swe-default-target}"
AGENT_SERVER_COMMAND="${OPENHANDS_AGENT_SERVER_COMMAND:-uvx --from openhands-agent-server==1.44.0 agent-server}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TRACE_DIR="${OPENHANDS_TRACE_DIR:-$ROOT/traces/openhands-multiturn-robust-$STAMP}"
WORKSPACE="${OPENHANDS_WORKSPACE:-/tmp/openhands-swe-turns-robust-$STAMP}"
PORT="${OPENHANDS_AGENT_PORT:-18080}"
PROXY_PORT="${OPENHANDS_PROXY_PORT:-18766}"

: "${OPENAI_API_KEY:?Set OPENAI_API_KEY before running this experiment.}"
for command in curl git python3; do
    command -v "$command" >/dev/null || { echo "Missing required command: $command" >&2; exit 1; }
done
[[ -d "$SOURCE_WORKSPACE/.git" ]] || { echo "Source workspace is not a Git checkout: $SOURCE_WORKSPACE" >&2; exit 1; }
[[ "$(git -C "$SOURCE_WORKSPACE" rev-parse HEAD)" == "8010edc761c98482aa804ad3d3c8447a09528715" ]] || {
    echo "Expected go-redis commit 8010edc761c98482aa804ad3d3c8447a09528715" >&2
    exit 1
}

mkdir -p "$TRACE_DIR"
cp -a "$SOURCE_WORKSPACE" "$WORKSPACE"
git -C "$WORKSPACE" status --porcelain | grep -q . && { echo "Copied workspace is unexpectedly dirty" >&2; exit 1; } || true
git -C "$WORKSPACE" rev-parse HEAD > "$TRACE_DIR/source-revision.txt"

session_key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
sampler_pid=""
proxy_pid=""
agent_pid=""
cleanup() {
    [[ -n "$sampler_pid" ]] && kill "$sampler_pid" 2>/dev/null || true
    [[ -n "$agent_pid" ]] && kill "$agent_pid" 2>/dev/null || true
    [[ -n "$proxy_pid" ]] && kill "$proxy_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

python3 "$ROOT/trace_proxy.py" --port "$PROXY_PORT" --trace "$TRACE_DIR/llm.jsonl" >"$TRACE_DIR/proxy.log" 2>&1 &
proxy_pid=$!
read -r -a agent_server <<< "$AGENT_SERVER_COMMAND"
SESSION_API_KEY="$session_key" OPENHANDS_SUPPRESS_BANNER=1 "${agent_server[@]}" --host 127.0.0.1 --port "$PORT" >"$TRACE_DIR/agent-server.log" 2>&1 &
agent_pid=$!
hardware_args=()
if command -v perf >/dev/null; then
    hardware_args=(--counter-root-pid "$agent_pid")
else
    echo "perf not found; hardware counters will be recorded as unavailable" >&2
fi

ready=0
for _ in $(seq 1 90); do
    if curl -fsS -H "X-Session-API-Key: $session_key" "http://127.0.0.1:$PORT/docs" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
[[ "$ready" == 1 ]] || { tail -n 100 "$TRACE_DIR/agent-server.log" >&2; exit 1; }

# Roots are explicit because the proxy is a sibling, not an agent-server child.
python3 "$ROOT/process_sampler.py" \
    --root-pid "$agent_pid" --root-pid "$proxy_pid" \
    --output "$TRACE_DIR/process-samples.jsonl" \
    --interval 0.01 --pss-interval 0.25 --discovery-interval 0.25 >"$TRACE_DIR/sampler.log" 2>&1 &
sampler_pid=$!

OPENAI_API_KEY="$OPENAI_API_KEY" OPENHANDS_SESSION_KEY="$session_key" \
python3 "$ROOT/run_openhands_multiturn.py" \
    --base-url "http://127.0.0.1:$PORT" \
    --llm-base-url "http://127.0.0.1:$PROXY_PORT/v1" \
    --trace-dir "$TRACE_DIR" --workspace "$WORKSPACE" \
    --timeout 120 --stall-timeout 25 --max-recoveries 2 \
    "${hardware_args[@]}"

kill "$sampler_pid" 2>/dev/null || true
wait "$sampler_pid" 2>/dev/null || true
sampler_pid=""
python3 "$ROOT/export_openhands_timeline.py" "$TRACE_DIR"
printf 'Trace written to %s\n' "$TRACE_DIR"
