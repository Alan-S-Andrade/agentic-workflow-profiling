# Sourced by smoke/sweep jobs: stage images to local disk, start vLLM, write agent config.
export MT=$WORK/sdebench-mt
cd $MT
module load tacc-apptainer >/dev/null 2>&1
RUN=${RUN:-$MT/runs/${SLURM_JOB_NAME}_${SLURM_JOB_ID}}
VLLM_OUT=${VLLM_OUT:-$RUN}
mkdir -p $RUN
export IMAGES_DIR=/tmp/$USER-images VM_CPUSET=${VM_CPUSET:-8-71}
mkdir -p $IMAGES_DIR
echo "== staging images to $IMAGES_DIR"
for i in $(cat ${IDS_FILE:-configs/instances_ok.txt}); do
  [[ -s $IMAGES_DIR/$i.ext4 ]] || numactl --membind=0 cp --sparse=always vm/images/$i.ext4 $IMAGES_DIR/ &
done; wait
du -sh $IMAGES_DIR
env/venv/bin/python configs/make_mini_config.py ${VLLM_PORT:-8000}
echo "== starting vLLM"; date
# If this vLLM build rejects --enable-prompt-tokens-details, retry without it rather than lose the job.
bash slurm/serve_vllm.sh $VLLM_OUT || { grep -q "unrecognized arguments" $VLLM_OUT/vllm.log && \
  PROMPT_DETAILS=0 bash slurm/serve_vllm.sh $VLLM_OUT; } || exit 1
export VLLM_URL=http://127.0.0.1:${VLLM_PORT:-8000}
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
grep -iE "KV cache|maximum concurrency|GPU blocks|model weights" $VLLM_OUT/vllm.log | tail -5
stop_vllm() { kill $(cat $VLLM_OUT/vllm.pid) 2>/dev/null; sleep 5; pkill -u $USER -f "vllm serve" 2>/dev/null; pkill -u $USER cloud-hypervisor 2>/dev/null; }
trap stop_vllm EXIT
