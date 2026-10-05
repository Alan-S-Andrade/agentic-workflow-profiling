#!/usr/bin/env bash
# Smoke test (port of slurm/serve_smoke.slurm) for one arm on a fresh vLLM server:
#   chat + bash tool-call sanity, offload_check.py (GPU->CPU->GPU KV migration), then unless
#   QUICK=1: one tenant end to end (N=1) and a 4-tenant concurrency check (N=4).
# Usage: smoke.sh [offload|baseline]   Output: runs/chi-smoke-<arm>_<timestamp>/
set -o pipefail
source "$(dirname "$0")/env.sh"
ARM=${1:-offload}
export RUN=$MT/runs/chi-smoke-${ARM}_$(date +%Y%m%d-%H%M)
export VLLM_OUT=$RUN
EXTRA_VLLM_ARGS=$(arm_args $ARM) || exit 2
export EXTRA_VLLM_ARGS
source $MT/slurm/common.sh
grep -iE "offload|connector" $VLLM_OUT/vllm.log | head -5
echo "== chat + tool-call sanity"
curl -s $VLLM_URL/v1/chat/completions -H 'Content-Type: application/json' -d '{
 "model":"qwen3.8-27b","max_tokens":2048,
 "messages":[{"role":"user","content":"List the files in the current directory using the bash tool."}],
 "tools":[{"type":"function","function":{"name":"bash","description":"Run a bash command","parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}}]}' \
 | env/venv/bin/python -c "import json,sys; r=json.load(sys.stdin); m=r['choices'][0]['message']; print('tool_calls:', m.get('tool_calls')); print('usage:', r['usage'])"
echo "== KV migration check"; date
env/venv/bin/python chameleon/offload_check.py --url $VLLM_URL > $RUN/offload_check.json; tail -2 $RUN/offload_check.json
[[ ${QUICK:-0} == 1 ]] && exit 0
echo "== N=1 single instance"; date
env/venv/bin/python sdebench_mt/driver.py -n 1 --jobs 1 --out $RUN/N1
echo "== N=4 concurrency check"; date
env/venv/bin/python sdebench_mt/driver.py -n 4 --jobs 4 --stagger-s 2 --out $RUN/N4
env/venv/bin/python -c "
import json; s=[json.loads(l) for l in open('$RUN/N4/samples.jsonl')]
print('max n_vmm', max(x['n_vmm'] for x in s), 'max running', max(x.get('vllm',{}).get('num_requests_running',0) for x in s))"
