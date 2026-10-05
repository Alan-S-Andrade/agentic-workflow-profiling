#!/bin/bash
# Serve Qwen3.8-27B (BF16) with vLLM on the GH200 node. Listens on localhost:8000 only;
# reach it from the workstation with: ssh -i chi-ncar-key -N -L 8000:localhost:8000 cc@<floating-ip>
set -euo pipefail

VENV="$HOME/vllm-env"
MODEL_DIR="${MODELS_DIR:-$HOME/models}/Qwen3.8-27B"

# FlashInfer's top-k/top-p sampler is JIT-compiled at first use, and neither nvcc on the
# node matches its headers (image: 12.6, venv cu13 wheel: 13.4), so use vLLM's own sampler.
export PATH="$VENV/bin:$PATH"
export VLLM_USE_FLASHINFER_SAMPLER=0

# --max-num-seqs: the default 1024 exceeds the 608 Mamba cache blocks available on 96 GB.
exec vllm serve "$MODEL_DIR" --served-model-name Qwen3.8-27B \
  --max-model-len 262144 --max-num-seqs 256 --gpu-memory-utilization 0.90 \
  --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_xml \
  --host 127.0.0.1 --port 8000
