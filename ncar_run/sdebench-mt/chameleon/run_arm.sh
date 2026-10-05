#!/usr/bin/env bash
# One N of one arm with a fresh vLLM server (port of slurm/sweep_dev.slurm, no Slurm time limit,
# so no dispatch deadline). Usage: run_arm.sh <offload|baseline|uva> <N> [jobs (default 2N)]
#   offload : vLLM native CPU KV-cache offloading, $OFFLOAD_GIB GiB of Grace memory
#             (blocks evicted from HBM migrate to CPU and are loaded back on a prefix hit)
#   baseline: GPU-only KV cache, as in the Vista runs in ghconfig.md
#   uva     : $UVA_GIB GiB of weights in Grace memory (--cpu-offload-gb), freed HBM added to the GPU KV cache
# All arms pin the GPU KV cache (chameleon/env.sh: KV_CACHE_BYTES, + UVA_GIB for uva).
# Output: runs/chi-<arm>_<SWEEP_ID>/N<n>/ (same layout as the Vista runs).
set -o pipefail
source "$(dirname "$0")/env.sh"
ARM=${1:?usage: run_arm.sh <offload|baseline|uva> <N> [jobs]} N=${2:?N} JOBS=${3:-$(( 2 * $2 ))}
export SWEEP_ID=${SWEEP_ID:-$(date +%Y%m%d-%H%M)}
export RUN=$MT/runs/chi-${ARM}_$SWEEP_ID
export VLLM_OUT=$RUN/N$N
EXTRA_VLLM_ARGS=$(arm_args $ARM) || exit 2
export EXTRA_VLLM_ARGS
if [[ -s $RUN/N$N/summary.json ]]; then echo "$ARM N=$N already complete, skipping"; exit 0; fi
mkdir -p $RUN/N$N
cat > $RUN/N$N/arm.json <<EOF
{"arm": "$ARM", "tenants": $N, "jobs": $JOBS, "extra_vllm_args": "$EXTRA_VLLM_ARGS",
 "host": "$(hostname)", "site": "CHI@NCAR", "driver": "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader)",
 "started": "$(date -Is)"}
EOF
# The previous (arm, N)'s server may still be releasing HBM: wait (up to 5 min) until the GPU is empty.
for i in $(seq 1 60); do
  (( $(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1) < 1024 )) && break
  sleep 5
done
source $MT/slurm/common.sh      # stage images, start vLLM (startup guard), write mini.yaml; trap stops vLLM
grep -iE "offload|kv_transfer|connector" $VLLM_OUT/vllm.log | grep -viE "warn.*deprecat" | head -5
echo "== $ARM N=$N jobs=$JOBS"; date
env/venv/bin/python sdebench_mt/driver.py -n $N --jobs $JOBS --out $RUN/N$N
# Prefill-cost calibration on the now idle server (after the run, so the run starts with empty caches).
env/venv/bin/python chameleon/prefill_cost.py --url $VLLM_URL --out $RUN/N$N/prefill_cost.json \
  || echo "prefill calibration failed (non-fatal)"
env/venv/bin/python sdebench_mt/analyze.py $RUN | tail -3
env/venv/bin/python chameleon/recompute.py $RUN | tail -4
env/venv/bin/python chameleon/timeline.py $RUN | tail -3   # KV + host-memory timeline per N
