#!/usr/bin/env bash
# Port of slurm/build_images.slurm for a Chameleon node: vLLM SIF (pinned by digest), instance
# metadata (dataset pinned by revision), the 19 instance images of configs/instances_ok.txt
# (pinned by digest), then the gold-patch check. Idempotent: skips what already exists.
set -o pipefail
source "$(dirname "$0")/env.sh"
cd $MT
export APPTAINER_CACHEDIR=/tmp/$USER-apcache APPTAINER_TMPDIR=/tmp/$USER-aptmp TMPDIR=/tmp/$USER-build
mkdir -p $APPTAINER_CACHEDIR $APPTAINER_TMPDIR $TMPDIR containers vm/images
IDS=${IDS_FILE:-configs/instances_ok.txt}

SIF=containers/vllm-openai-v0.30.0.sif
VLLM_REF=docker://vllm/vllm-openai@sha256:4864d46625cbc3307623e29ac742030655e27249feba7b97ec925ce4cc4dfb56
if [[ ! -s $SIF ]]; then
  (apptainer pull $SIF.partial $VLLM_REF > containers/pull.log 2>&1 && mv $SIF.partial $SIF \
     && echo "VLLM_SIF=ok $(sha256sum $SIF | cut -c1-16)") || echo "VLLM_SIF=failed (see containers/pull.log)" &
fi

echo "== instance metadata (SWE-bench_Verified @ 78f471bf)"
HF_HOME=$MT/models IDS=$IDS env/venv/bin/python - <<'PY'
import json, os
from datasets import load_dataset
ids = [l.strip() for l in open(os.environ["IDS"]) if l.strip()]
ds = load_dataset("SWE-bench/SWE-bench_Verified", split="test",
                  revision="78f471bf655a3137b2e8a75af1501690ec009ec3")
ds = {x["instance_id"]: x for x in ds}
with open("configs/instances.jsonl", "w") as f:
    for i in ids:
        f.write(json.dumps(ds[i]) + "\n")
print("wrote", len(ids), "instances")
PY

echo "== instance images"
xargs -a $IDS -P ${PAR:-6} -I{} bash -c 'bash vm/build_instance.sh {} && echo "IMG_OK {}" || echo "IMG_FAIL {}"'
wait
ls -la vm/images/*.ext4 | awk '{s+=$5} END {print NR " images, " s/1e9 " GB apparent"}'
du -sh vm/images

echo "== gold-patch check (every instance must resolve)"
mkdir -p configs/gold
xargs -a $IDS -P ${GOLD_PAR:-6} -I{} bash -c \
  '[[ -s configs/gold/{}.jsonl ]] || env/venv/bin/python sdebench_mt/gold_check.py {} --out configs/gold/{}.jsonl'
cat configs/gold/*.jsonl > configs/gold_check.jsonl
env/venv/bin/python -c "
import json; r=[json.loads(l) for l in open('configs/gold_check.jsonl')]
print(f'GOLD resolved {sum(x[\"resolved\"] for x in r)}/{len(r)}'); [print('  NOT RESOLVED', x['instance_id'], x['reason']) for x in r if not x['resolved']]"
