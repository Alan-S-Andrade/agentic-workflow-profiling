#!/usr/bin/env bash
# Full experiment: for N in 16 32, run the offload arm then the baseline arm, each with a fresh
# vLLM server and 2N instance runs; then back up the results to the object store.
# Env: NS (default "16 32"), ARMS (default "offload baseline"), SWEEP_ID, OFFLOAD_GIB (default 64).
# Run detached, e.g.:  nohup chameleon/sweep.sh > ~/sweep.log 2>&1 < /dev/null &
set -o pipefail
source "$(dirname "$0")/env.sh"
export SWEEP_ID=${SWEEP_ID:-$(date +%Y%m%d-%H%M)}
echo "SWEEP_ID=$SWEEP_ID  NS=${NS:-16 32}  ARMS=${ARMS:-offload baseline}  OFFLOAD_GIB=$OFFLOAD_GIB"
for N in ${NS:-16 32}; do
  for ARM in ${ARMS:-offload baseline}; do
    bash $MT/chameleon/run_arm.sh $ARM $N || echo "!! $ARM N=$N failed (exit $?)"
  done
done
bash $MT/chameleon/store.sh push-runs
