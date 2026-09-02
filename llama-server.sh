#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLAMA_DIR="${LLAMA_CPP_DIR:-$ROOT/.tools/llama.cpp}"
MODEL="${LLAMA_MODEL:-$ROOT/.models/Llama-3.2-3B-Instruct-Q4_K_M.gguf}"
PORT="${LLAMA_PORT:-18080}"

if [[ ! -x "$LLAMA_DIR/build/bin/llama-server" && ! -x "$LLAMA_DIR/build/bin/llama-server.exe" ]]; then
  command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
  command -v cmake >/dev/null || { echo "cmake is required to build llama.cpp" >&2; exit 1; }
  if [[ ! -d "$LLAMA_DIR/.git" ]]; then
    mkdir -p "$(dirname "$LLAMA_DIR")"
    git clone --depth 1 https://github.com/ggml-org/llama.cpp.git "$LLAMA_DIR"
  fi
  cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DGGML_NATIVE=ON -DLLAMA_CURL=OFF
  cmake --build "$LLAMA_DIR/build" --config Release -j "${LLAMA_BUILD_JOBS:-2}"
fi

SERVER="$LLAMA_DIR/build/bin/llama-server"
[[ -x "$SERVER" ]] || SERVER="$LLAMA_DIR/build/bin/llama-server.exe"
[[ -f "$MODEL" ]] || {
  echo "GGUF model not found: $MODEL" >&2
  echo "Download one, then rerun. Example:" >&2
  echo "  uvx --from huggingface_hub hf download bartowski/Llama-3.2-3B-Instruct-GGUF Llama-3.2-3B-Instruct-Q4_K_M.gguf --local-dir $ROOT/.models" >&2
  exit 1
}

exec "$SERVER" --model "$MODEL" --alias local-llama --host 127.0.0.1 --port "$PORT" --ctx-size "${LLAMA_CTX_SIZE:-4096}" --parallel "${LLAMA_PARALLEL:-1}" "$@"
