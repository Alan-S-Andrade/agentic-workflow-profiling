#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"
output=${1:-traces/tier-comparison-100-2s-20261021/phase-priority-proactive-4p5s}
schedule=${2:-traces/tier-comparison-100-2s-20261021/launch-schedule-4p5s.json}
endpoint=${MICROVM_INFERENCE_ENDPOINT:-http://128.110.220.114:8081/v1/inference}
proactive_percent=75
guard_percent=90

if [[ -e "$output" ]]; then
  echo "output already exists: $output" >&2
  exit 2
fi

original_numa_balancing=$(cat /proc/sys/kernel/numa_balancing)
original_demotion=$(cat /sys/kernel/mm/numa/demotion_enabled)
controller_pid=
sampler_pid=
restore_policy() {
  if [[ -n "$sampler_pid" ]]; then
    wait "$sampler_pid" 2>/dev/null || true
  fi
  if [[ -n "$controller_pid" ]]; then
    kill -INT "$controller_pid" 2>/dev/null || true
    wait "$controller_pid" 2>/dev/null || true
  fi
  sysctl -w "kernel.numa_balancing=$original_numa_balancing" >/dev/null
  printf '%s\n' "$original_demotion" > /sys/kernel/mm/numa/demotion_enabled
}
trap restore_policy EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Whole-VM migration owns NUMA placement during this experiment.
sysctl -w kernel.numa_balancing=0
printf 'false\n' > /sys/kernel/mm/numa/demotion_enabled

python3 -u microvm_phase_lru_tiering_proactive.py \
  --trace-root "$output" \
  --phase-dir "$output/sessions-100/phases" \
  --fast-node 0 --slow-node 1 \
  --demote-free-gib 60 --promote-free-gib 80 --slow-reserve-gib 3 \
  --system-memory-demote-percent "$proactive_percent" \
  --memory-guard-percent "$guard_percent" \
  > "${output}-phase-priority-events.jsonl" 2>&1 &
controller_pid=$!
sleep 1

# Keep execution on node-0 CPUs after a VM's guest RAM moves to node 1.
export PATH="$PWD/.tools:$PATH"
export MICROVM_NUMA_NODES=0,1
export MICROVM_CPUSET="$(cat /sys/devices/system/node/node0/cpulist)"
numactl --cpunodebind=0 python3 swebench_microvm_scale_experiment.py \
  --output "$output" \
  --inference-endpoint "$endpoint" \
  --admission-schedule "$schedule" \
  --stages 100 --memory-threshold-percent "$guard_percent" \
  --fast-node-min-free-gib 10 --arch arm64 --slurm-vsock --tiered-memory \
  --guest-vcpus 4 --replay-inference-latency --run &
run_pid=$!
while [[ ! -d "$output/sessions-100/vms" ]] && kill -0 "$run_pid" 2>/dev/null; do
  sleep 1
done
if kill -0 "$run_pid" 2>/dev/null; then
  python3 -u record_tpp_migrations.py --run-pid "$run_pid" \
    --vm-root "$output/sessions-100/vms" --output "${output}-residency.jsonl" \
    --interval 2 > "${output}-residency.log" 2>&1 &
  sampler_pid=$!
fi
wait "$run_pid"
