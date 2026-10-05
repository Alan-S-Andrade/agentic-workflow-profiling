#!/bin/bash
#SBATCH -J sdebench-dev
#SBATCH -p gh-dev
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -c 72
#SBATCH -t 02:00:00
#SBATCH -A ASC26114
#SBATCH -o sweep_dev_%j.out
# Tenant-count sweep split for gh-dev (2 h max; 1 running / 3 submitted jobs per user).
# One N per job, fresh vLLM per job, all jobs write into runs/sdebench-sweep-dev_<SWEEP_ID>/N<n>.
# TACC forbids sbatch from compute nodes, so each IDX is submitted from a login node
# (at most 3 at a time: running + pending count toward gh-dev's per-user submit limit).
# Env: SWEEP_ID, IDX, CHUNKS (colon-separated N list), JOBS (colon-separated instance runs per N).
set -o pipefail
CHUNKS=${CHUNKS:-1:2:4:8:16:32}
JOBS=${JOBS:-8:8:8:16:32:32}
IFS=: read -ra NS_ <<< "$CHUNKS"
IFS=: read -ra JOBS_ <<< "$JOBS"
N=${NS_[$IDX]}
JOB_T0=$(date +%s)
export RUN=$WORK/sdebench-mt/runs/sdebench-sweep-dev_$SWEEP_ID
export VLLM_OUT=$RUN/N$N
if [[ -s $RUN/N$N/summary.json ]]; then echo "N=$N already complete, skipping"; exit 0; fi
source $WORK/sdebench-mt/slurm/common.sh
echo "== N=$N jobs=${JOBS_[$IDX]}"; date
# No new instance after 85 min of job time so in-flight ones finish inside the 2 h limit.
env/venv/bin/python sdebench_mt/driver.py -n $N --jobs ${JOBS_[$IDX]} --out $RUN/N$N \
  --dispatch-deadline $(( JOB_T0 + 85 * 60 ))
env/venv/bin/python sdebench_mt/analyze.py $RUN | tail -3
