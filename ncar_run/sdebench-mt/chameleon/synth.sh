#!/usr/bin/env bash
# Validation of the recompute measurement (not a benchmark): synthetic multi-turn load (synth_load.py)
# on a fresh server for one arm, then prefill calibration, analyze.py and recompute.py.
# Usage: synth.sh <offload|baseline> [tenants=32] [duration_s=900]   Output: runs/chi-synth-<arm>_<ts>/N<n>/
set -o pipefail
source "$(dirname "$0")/env.sh"
ARM=${1:?usage: synth.sh <offload|baseline> [tenants] [duration_s]} N=${2:-32} DUR=${3:-900}
export RUN=$MT/runs/chi-synth-${ARM}_$(date +%Y%m%d-%H%M)
export VLLM_OUT=$RUN/N$N
EXTRA_VLLM_ARGS=$(arm_args $ARM) || exit 2
export EXTRA_VLLM_ARGS
mkdir -p $RUN/N$N
echo "{\"arm\": \"$ARM\", \"tenants\": $N, \"synthetic\": true, \"extra_vllm_args\": \"$EXTRA_VLLM_ARGS\"}" > $RUN/N$N/arm.json
source $MT/slurm/common.sh
env/venv/bin/python chameleon/synth_load.py --url $VLLM_URL --out $RUN/N$N --tenants $N --duration-s $DUR
env/venv/bin/python chameleon/prefill_cost.py --url $VLLM_URL --out $RUN/N$N/prefill_cost.json
env/venv/bin/python sdebench_mt/analyze.py $RUN | tail -3
env/venv/bin/python chameleon/recompute.py $RUN
