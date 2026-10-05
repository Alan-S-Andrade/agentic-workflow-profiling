#!/usr/bin/env bash
# Sync the SDEBench benchmark data with the CHI@NCAR object store (rclone remote "chi-ncar",
# container "sdebench-mt"). Model weights live separately in llm-weights (~/bin/weights.sh).
#   push       code + configs, vm/bin, vm/kernel, vLLM SIF, instance images (zstd), runs/
#   pull       the same back onto a fresh node (images are decompressed sparse)
#   push-runs  only runs/
#   ls         sizes per prefix
# Layout in the container:
#   code/  (repo without env, models, images, containers, runs)   vm/bin/  vm/kernel/
#   containers/vllm-openai-v0.30.0.sif   images/<instance>.ext4.zst   runs/<run>/...
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd $MT
R=chi-ncar:sdebench-mt
OPTS=(--transfers 8 --checkers 16 --stats 60s --stats-one-line)
ZDIR=$MT/vm/images.zst   # local cache of compressed images (ext4 images are mostly sparse zeros)

push_code() {
  rclone copy . $R/code "${OPTS[@]}" --exclude "env/**" --exclude "models/**" --exclude "models" \
    --exclude "vm/images/**" --exclude "vm/images.zst/**" --exclude "vm/bin/**" --exclude "vm/kernel/**" \
    --exclude "containers/**" --exclude "runs/**" --exclude "__pycache__/**"
  rclone copy vm/bin $R/vm/bin "${OPTS[@]}"
  rclone copy vm/kernel $R/vm/kernel "${OPTS[@]}"
}
push_images() {
  mkdir -p $ZDIR
  for img in vm/images/*.ext4; do
    z=$ZDIR/$(basename $img).zst
    [[ -s $z && $z -nt $img ]] || zstd -q -f -T16 -3 "$img" -o "$z"
  done
  rclone copy $ZDIR $R/images "${OPTS[@]}" --include "*.ext4.zst"
  rclone copy containers $R/containers "${OPTS[@]}" --include "*.sif"
}
push_runs() { [[ -d runs ]] && rclone copy runs $R/runs "${OPTS[@]}" --exclude "__pycache__/**"; }

case ${1:-} in
  push)
    rclone mkdir $R
    push_code; push_images; push_runs
    rclone check vm/bin $R/vm/bin --one-way && rclone check $ZDIR $R/images --one-way --include "*.ext4.zst"
    ;;
  push-runs) rclone mkdir $R; push_runs ;;
  pull)
    rclone copy $R/code . "${OPTS[@]}"
    rclone copy $R/vm/bin vm/bin "${OPTS[@]}"; rclone copy $R/vm/kernel vm/kernel "${OPTS[@]}"
    chmod +x vm/bin/* chameleon/*.sh chameleon/bin/* 2>/dev/null || true
    rclone copy $R/containers containers "${OPTS[@]}"
    rclone copy $R/images $ZDIR "${OPTS[@]}"
    mkdir -p vm/images
    for z in $ZDIR/*.ext4.zst; do
      img=vm/images/$(basename $z .zst)
      [[ -s $img ]] || zstd -q -d --sparse "$z" -o "$img"
    done
    ls vm/images/*.ext4 | wc -l | xargs echo "images:"
    ;;
  ls) rclone size $R; for p in code vm containers images runs; do echo -n "$p: "; rclone size $R/$p 2>/dev/null | tr '\n' ' '; echo; done ;;
  *) echo "usage: $0 {push|pull|push-runs|ls}"; exit 2 ;;
esac
