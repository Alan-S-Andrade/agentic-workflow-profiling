#!/usr/bin/env bash
set -Eeuo pipefail

cd /users/alanuiuc/agentic-workflow-profiling
output=traces/remote-swebench-phase-priority-whole-vm-node0-compute-azure-200x-100-20261021-retry1
schedule=traces/azure-yig-admission-100-200x-20261021/launch-schedule.json
endpoint=http://128.110.220.114:8081/v1/inference

if [[ -e "$output" ]]; then
  echo "output already exists: $output" >&2
  exit 2
fi

original_numa_balancing=$(cat /proc/sys/kernel/numa_balancing)
original_demotion=$(cat /sys/kernel/mm/numa/demotion_enabled)
controller_pid=
restore_policy() {
  if [[ -n "$controller_pid" ]]; then
    kill -INT "$controller_pid" 2>/dev/null || true
    wait "$controller_pid" 2>/dev/null || true
  fi
  sysctl -w "kernel.numa_balancing=$original_numa_balancing" >/dev/null
  printf '%s\n' "$original_demotion" > /sys/kernel/mm/numa/demotion_enabled
}
trap restore_policy EXIT INT TERM

# Whole-VM migration owns NUMA placement during this experiment.
sysctl -w kernel.numa_balancing=0
printf 'false\n' > /sys/kernel/mm/numa/demotion_enabled

python3 -u microvm_phase_lru_tiering.py \
  --trace-root "$output" \
  --phase-dir "$output/sessions-100/phases" \
  --fast-node 0 --slow-node 1 \
  > "${output}-phase-priority-events.jsonl" 2>&1 &
controller_pid=$!
sleep 1

# Do not pass --move-cpus-with-memory: VM execution stays on node-0 CPUs even
# after the controller moves guest RAM to slow node 1.
export PATH="$PWD/.tools:$PATH"
export MICROVM_NUMA_NODES=0,1
export MICROVM_CPUSET="$(cat /sys/devices/system/node/node0/cpulist)"
numactl --cpunodebind=0 python3 swebench_microvm_scale_experiment.py \
  --output "$output" \
  --inference-endpoint "$endpoint" \
  --admission-schedule "$schedule" \
  --stages 100 --run
