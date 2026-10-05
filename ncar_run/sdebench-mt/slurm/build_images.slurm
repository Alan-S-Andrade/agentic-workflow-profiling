#!/bin/bash
#SBATCH -J sdebench-images
#SBATCH -p gh-dev
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -c 72
#SBATCH -t 02:00:00
#SBATCH -A ASC26114
#SBATCH -o build_images_%j.out
# Step 3: per-instance guest rootfs images + vLLM container. Idempotent (skips existing).
set -o pipefail
export MT=$WORK/sdebench-mt
cd $MT
module load tacc-apptainer
export APPTAINER_CACHEDIR=/tmp/$USER-apcache APPTAINER_TMPDIR=/tmp/$USER-aptmp TMPDIR=/tmp/$USER-build
mkdir -p $APPTAINER_CACHEDIR $APPTAINER_TMPDIR $TMPDIR
VLLM_TAG=${VLLM_TAG:-v0.30.0}
SIF=$MT/containers/vllm-openai-$VLLM_TAG.sif
if [[ ! -s $SIF ]]; then
  (apptainer pull $SIF.partial docker://vllm/vllm-openai:$VLLM_TAG > $MT/containers/pull.log 2>&1 \
     && mv $SIF.partial $SIF && echo "VLLM_SIF=ok") || echo "VLLM_SIF=failed" &
fi
# Instance metadata for runtime (problem statement, eval script, tests).
HF_HOME=$MT/models env/venv/bin/python - <<'PY'
import json
from datasets import load_dataset
ids = [l.strip() for l in open("configs/instances.txt") if l.strip()]
ds = {x["instance_id"]: x for x in load_dataset("SWE-bench/SWE-bench_Verified", split="test")}
with open("configs/instances.jsonl", "w") as f:
    for i in ids:
        f.write(json.dumps(ds[i]) + "\n")
print("wrote", len(ids), "instances")
PY
xargs -a configs/instances.txt -P ${PAR:-6} -I{} bash -c 'bash vm/build_instance.sh {} && echo "IMG_OK {}" || echo "IMG_FAIL {}"'
wait
ls -la vm/images/*.ext4 | awk '{s+=$5} END {print NR " images, " s/1e9 " GB apparent"}'
du -sh vm/images
