#!/usr/bin/env bash
# Chained after the main sweep (offload + baseline arms): install the staged code (new sampler fields,
# uva arm, timeline.py), smoke-test the uva arm, run it for N=16 and 32 under the same SWEEP_ID, draw the
# timelines of every arm of that sweep and push the runs to the object store.
# Usage: phase2.sh <pid of the running sweep.sh> <staging dir> <SWEEP_ID>
# Run detached:  setsid nohup bash phase2.sh <pid> ~/sdebench-mt-next <id> > ~/phase2.log 2>&1 < /dev/null &
set -o pipefail
PID=${1:?pid} STAGE=${2:?staging dir} SID=${3:?SWEEP_ID}
echo "== waiting for sweep pid $PID ($(date -u +%T))"
while kill -0 "$PID" 2>/dev/null; do sleep 60; done
echo "== main sweep finished $(date -u +%T); installing $STAGE"
rsync -a --exclude "__pycache__/" "$STAGE"/ "$HOME"/sdebench-mt/
cd "$HOME"/sdebench-mt
source chameleon/env.sh
echo "== uva smoke test ($(arm_args uva))"
QUICK=1 bash chameleon/smoke.sh uva || { echo "!! uva smoke test failed, stopping"; exit 1; }
SWEEP_ID=$SID ARMS=uva NS="16 32" bash chameleon/sweep.sh
for R in runs/chi-*_"$SID"; do env/venv/bin/python chameleon/timeline.py "$R" | tail -2; done
bash chameleon/store.sh push-runs
echo "PHASE2-DONE $(date -u +%T)"
