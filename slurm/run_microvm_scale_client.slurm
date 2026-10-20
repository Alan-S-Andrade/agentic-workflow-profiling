#!/usr/bin/env bash
# Rootless GH200/Vista full scale-client sweep.
# Submit from the repository root on a TACC login node:
#   sbatch slurm/run_microvm_scale_client.slurm
# Override the server with: sbatch --export=ALL,SERVER_IP=1.2.3.4 slurm/run_microvm_scale_client.slurm
# Slurm directives below match the working Vista GH200 recipes in ncar_run.
#SBATCH -J microvm-scale
#SBATCH -p gh
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -c 72
#SBATCH -t 24:00:00
#SBATCH -A ASC26114
#SBATCH -o microvm-scale-%j.out
set -euo pipefail

ROOT="${SLURM_SUBMIT_DIR:?Submit from the repository root}"
SERVER_IP="${SERVER_IP:-128.110.220.114}"
OUT="$ROOT/traces/microvm-scale-${SLURM_JOB_ID}"

cd "$ROOT"
mkdir -p "$ROOT/traces"
export TMPDIR="/tmp/$USER-microvm-$SLURM_JOB_ID"
export APPTAINER_TMPDIR="$TMPDIR/apptainer-tmp"
export APPTAINER_CACHEDIR="$TMPDIR/apptainer-cache"
export MICROVM_VSOCK_DIR="/tmp/$USER-microvm-vsock-$SLURM_JOB_ID"
mkdir -p "$TMPDIR" "$MICROVM_VSOCK_DIR"

bash "$ROOT/setup_microvm_scale_client_slurm.sh" "$SERVER_IP"
source "$ROOT/cloud-hypervisor.env"

echo "Job: $SLURM_JOB_ID on $(hostname)"
echo "Endpoint: $MICROVM_INFERENCE_ENDPOINT"
echo "Output: $OUT"
test -r /dev/kvm -a -w /dev/kvm

python3 "$ROOT/swebench_microvm_scale_experiment.py" \
  --output "$OUT" \
  --inference-endpoint "$MICROVM_INFERENCE_ENDPOINT" \
  --stages 1 2 4 8 16 32 64 128 \
  --artifact-arch arm64 \
  --sample-interval-seconds "${SAMPLE_INTERVAL_SECONDS:-0.5}" \
  --memory-threshold-percent "${MEMORY_THRESHOLD_PERCENT:-90}" \
  --allow-swap \
  --slurm-vsock \
  --run
