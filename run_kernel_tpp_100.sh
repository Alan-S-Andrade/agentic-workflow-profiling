#!/usr/bin/env bash
set -Eeuo pipefail

cd /users/alanuiuc/agentic-workflow-profiling
output=traces/remote-swebench-kernel-tpp-node0-compute-azure-200x-100-20261021
schedule=traces/azure-yig-admission-100-200x-20261021/launch-schedule.json
endpoint=http://128.110.220.114:8081/v1/inference

if [[ -e "$output" ]]; then
  echo "output already exists: $output" >&2
  exit 2
fi

original_numa_balancing=$(cat /proc/sys/kernel/numa_balancing)
original_demotion=$(cat /sys/kernel/mm/numa/demotion_enabled)
original_scale_factor=$(cat /proc/sys/vm/demote_scale_factor)
runner_pid=
sampler_pid=
sampler_output="${output}-tpp-migration-samples.jsonl"

restore_tpp() {
  if [[ -n "$sampler_pid" ]]; then
    kill "$sampler_pid" 2>/dev/null || true
    wait "$sampler_pid" 2>/dev/null || true
  fi
  if [[ -n "$runner_pid" ]] && kill -0 "$runner_pid" 2>/dev/null; then
    kill -INT "$runner_pid" 2>/dev/null || true
    wait "$runner_pid" 2>/dev/null || true
  fi
  sysctl -w kernel.numa_balancing=0 >/dev/null
  printf '%s\n' "$original_demotion" > /sys/kernel/mm/numa/demotion_enabled
  printf '%s\n' "$original_scale_factor" > /proc/sys/vm/demote_scale_factor
  sysctl -w "kernel.numa_balancing=$original_numa_balancing" >/dev/null
}
trap restore_tpp EXIT INT TERM

python3 enable_kernel_tpp.py check
python3 enable_kernel_tpp.py enable

export PATH="$PWD/.tools:$PATH"
export MICROVM_NUMA_NODES=0,1
export MICROVM_CPUSET="$(cat /sys/devices/system/node/node0/cpulist)"

# Keep MPOL_DEFAULT and allow both nodes in the inherited memory set. This
# leaves memory placement and page migration to the patched TPP kernel.
numactl --cpunodebind=0 python3 swebench_microvm_scale_experiment.py \
  --output "$output" \
  --inference-endpoint "$endpoint" \
  --admission-schedule "$schedule" \
  --stages 100 --run &
runner_pid=$!

python3 -u record_tpp_migrations.py \
  --run-pid "$runner_pid" \
  --vm-root "$output" \
  --output "$sampler_output" \
  --interval 2 &
sampler_pid=$!

status=0
wait "$runner_pid" || status=$?
runner_pid=
wait "$sampler_pid" || true
sampler_pid=
if [[ -f "$sampler_output" ]]; then
  mv "$sampler_output" "$output/tpp-migration-samples.jsonl"
fi
exit "$status"
