#!/bin/bash
# Store/fetch model weights between the node's local disk and the CHI@NCAR object store.
# Usage: weights.sh {push|pull|check} [model]   (default model: Qwen/Qwen3.8-27B)
# Needs the "chi-ncar" rclone remote in ~/.config/rclone/rclone.conf
set -euo pipefail

MODEL="${2:-Qwen/Qwen3.8-27B}"
LOCAL="${MODELS_DIR:-$HOME/models}/${MODEL##*/}"
REMOTE="chi-ncar:llm-weights/$MODEL"
OPTS=(--exclude ".cache/**" --transfers 8 --checkers 16)

case "${1:-}" in
  push)
    rclone mkdir chi-ncar:llm-weights
    rclone copy "$LOCAL" "$REMOTE" "${OPTS[@]}" --progress
    rclone check "$LOCAL" "$REMOTE" "${OPTS[@]}" --one-way ;;
  pull)
    mkdir -p "$LOCAL"
    rclone copy "$REMOTE" "$LOCAL" "${OPTS[@]}" --multi-thread-streams 8 --progress
    rclone check "$REMOTE" "$LOCAL" "${OPTS[@]}" --one-way ;;
  check)
    rclone size "$REMOTE"
    rclone check "$LOCAL" "$REMOTE" "${OPTS[@]}" ;;
  *)
    echo "usage: $0 {push|pull|check} [hf-org/model]" >&2; exit 1 ;;
esac
