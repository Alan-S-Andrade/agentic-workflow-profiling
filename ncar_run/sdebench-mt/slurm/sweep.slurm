#!/bin/bash
#SBATCH -J sdebench-sweep
#SBATCH -p gh
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -c 72
#SBATCH -t 12:00:00
#SBATCH -A ASC26114
#SBATCH -o sweep_%j.out
# Step 6: tenant-count sweep against one shared vLLM server (GPU-only KV cache, no CPU
# offload). One server for all N; results are written per N as they complete.
source $WORK/sdebench-mt/slurm/common.sh
for N in ${NS:-1 2 4 8 16 32}; do
  echo "== N=$N"; date
  env/venv/bin/python sdebench_mt/driver.py -n $N --jobs ${JOBS_PER_N:-$(( N < 8 ? 8 : 2*N ))} --out $RUN/N$N
done
env/venv/bin/python sdebench_mt/analyze.py $RUN
