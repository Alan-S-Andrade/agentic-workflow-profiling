#!/usr/bin/env bash
# Start the shared Qwen3.8-27B vLLM server on this GH200 node (background), wait until healthy.
# Env knobs: VLLM_TAG, VLLM_PORT, MAX_MODEL_LEN, MTP=1, GPU_UTIL, VLLM_CPUS, EXTRA_VLLM_ARGS, HEALTH_TIMEOUT_S
set -eo pipefail
MT=${MT:-$WORK/sdebench-mt}
OUT=${1:?usage: serve_vllm.sh <log_dir>}
VLLM_TAG=${VLLM_TAG:-v0.30.0}
PORT=${VLLM_PORT:-8000}
SIF=$MT/containers/vllm-openai-$VLLM_TAG.sif
spec=()
[[ ${MTP:-0} == 1 ]] && spec=(--speculative-config '{"method":"mtp","num_speculative_tokens":3}')
# usage.prompt_tokens_details.cached_tokens per call -> per-tenant prefix reuse / eviction
details=(); [[ ${PROMPT_DETAILS:-1} == 1 ]] && details=(--enable-prompt-tokens-details)
MIN_KV_TOKENS=${MIN_KV_TOKENS:-780000}
module load tacc-apptainer >/dev/null 2>&1
mkdir -p "$OUT" /tmp/$USER-vllm-cache

# GH200: HBM is NUMA node 1, so file page cache that lands there counts as used GPU memory
# and shrinks the KV cache (seen: 2.1 GiB instead of 26.6 GiB). Evict our files' cache first.
mem_report() {
  echo "$1: node0/node1 FilePages MB = $(numastat -m | awk '/^FilePages/{print $2"/"$3}'), GPU used = $(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
}
evict() {
  python3 $MT/slurm/evict_pagecache.py ${IMAGES_DIR:-} $MT/models/Qwen3.8-27B
  numactl --membind=1 python3 $MT/slurm/flush_hbm_pagecache.py 1
}

launch() {
# vLLM gets its own cores (default 0-7) so tenant VMs don't perturb the scheduler loop;
# its host allocations and the weights' page cache prefer NUMA node 0 (not the GPU's HBM).
taskset -c ${VLLM_CPUS:-0-7} numactl --preferred=0 apptainer exec --nv --cleanenv \
  --bind $MT/models:/models --bind /tmp/$USER-vllm-cache:/cache \
  --env HF_HUB_OFFLINE=1,HOME=/cache,VLLM_CACHE_ROOT=/cache/vllm,XDG_CACHE_HOME=/cache,TRITON_CACHE_DIR=/cache/triton,TORCHINDUCTOR_CACHE_DIR=/cache/inductor \
  $SIF vllm serve /models/Qwen3.8-27B \
    --served-model-name qwen3.8-27b --host 127.0.0.1 --port $PORT \
    --max-model-len ${MAX_MODEL_LEN:-131072} --kv-cache-dtype fp8 \
    --gpu-memory-utilization ${GPU_UTIL:-0.85} --enable-prefix-caching \
    --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser ${TOOL_PARSER:-qwen3_xml} \
    --limit-mm-per-prompt '{"image":0,"video":0}' --max-num-seqs ${MAX_NUM_SEQS:-64} \
    "${spec[@]}" "${details[@]}" ${EXTRA_VLLM_ARGS:-} > "$OUT/vllm.log" 2>&1 &
echo $! > "$OUT/vllm.pid"
for i in $(seq 1 $(( ${HEALTH_TIMEOUT_S:-900} / 5 ))); do
  curl -sf http://127.0.0.1:$PORT/health >/dev/null && { echo "vLLM healthy after $((i*5))s"; return 0; }
  kill -0 $(cat "$OUT/vllm.pid") 2>/dev/null || { echo "vLLM died"; tail -50 "$OUT/vllm.log"; return 1; }
  sleep 5
done
echo "vLLM not healthy after ${HEALTH_TIMEOUT_S:-900}s"; tail -50 "$OUT/vllm.log"; return 1
}

kv_tokens() { grep -oE "GPU KV cache size: [0-9,]+ tokens" "$OUT/vllm.log" | tail -1 | grep -oE "[0-9,]+" | tr -d ,; }

# Every N must get the same KV capacity (~810K tokens) for the sweep to be comparable.
for attempt in 1 2; do
  mem_report "before evict"; evict; mem_report "after evict"
  launch; ok=$?
  kv=$(kv_tokens)
  echo "KV cache tokens: ${kv:-unknown} (minimum $MIN_KV_TOKENS), attempt $attempt"
  if (( ok == 0 )) && [[ -n $kv ]] && (( kv >= MIN_KV_TOKENS )); then exit 0; fi
  kill $(cat "$OUT/vllm.pid") 2>/dev/null; sleep 10; pkill -u $USER -f "vllm serve" 2>/dev/null; sleep 5
  cp "$OUT/vllm.log" "$OUT/vllm.attempt$attempt.log"
  grep -qiE "not enough|larger than the available KV cache" "$OUT/vllm.log" || (( ok == 0 )) || exit 1
done
echo "vLLM KV cache below $MIN_KV_TOKENS tokens after retry"; exit 1
