#!/bin/bash
#SBATCH -J sdebench-smoke
#SBATCH -p gh-dev
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -c 72
#SBATCH -t 02:00:00
#SBATCH -A ASC26114
#SBATCH -o smoke_%j.out
# Step 5: vLLM + one tenant end to end, then a 4-tenant concurrency check.
source $WORK/sdebench-mt/slurm/common.sh
echo "== chat + tool-call sanity"
curl -s $VLLM_URL/v1/chat/completions -H 'Content-Type: application/json' -d '{
 "model":"qwen3.8-27b","max_tokens":2048,
 "messages":[{"role":"user","content":"List the files in the current directory using the bash tool."}],
 "tools":[{"type":"function","function":{"name":"bash","description":"Run a bash command","parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}}]}' \
 | env/venv/bin/python -c "import json,sys; r=json.load(sys.stdin); m=r['choices'][0]['message']; print('tool_calls:', m.get('tool_calls')); print('usage:', r['usage']); print('reasoning chars:', len(m.get('reasoning_content') or m.get('reasoning') or ''))"
echo "== N=1 single instance"; date
env/venv/bin/python sdebench_mt/driver.py -n 1 --jobs 1 --out $RUN/N1
echo "== N=4 concurrency check"; date
env/venv/bin/python sdebench_mt/driver.py -n 4 --jobs 4 --stagger-s 2 --out $RUN/N4
env/venv/bin/python -c "
import json; s=[json.loads(l) for l in open('$RUN/N4/samples.jsonl')]
print('max n_vmm', max(x['n_vmm'] for x in s), 'max running', max(x.get('vllm',{}).get('num_requests_running',0) for x in s))"
