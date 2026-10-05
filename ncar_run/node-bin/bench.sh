#!/bin/bash
# Benchmark the running vLLM server (serve.sh) on the node with `vllm bench serve`.
# Random fixed-length prompts, fixed output length (--ignore-eos), greedy decoding.
# Results: ~/bench/<timestamp>/{*.json,*.log,gpu.csv}
set -u
export PATH="$HOME/vllm-env/bin:$PATH"
OUT="$HOME/bench/$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT"

COMMON=(--backend vllm --base-url http://127.0.0.1:8000 --model Qwen3.8-27B
        --tokenizer "$HOME/models/Qwen3.8-27B" --dataset-name random --random-range-ratio 0
        --ignore-eos --temperature 0 --seed 42 --num-warmups 2
        --percentile-metrics ttft,tpot,itl,e2el --metric-percentiles 50,90,99
        --save-result --result-dir "$OUT")

run() {
  local name=$1; shift
  echo "$(date +%T) start $name"
  vllm bench serve "${COMMON[@]}" --result-filename "$name.json" "$@" > "$OUT/$name.log" 2>&1
  echo "$(date +%T) done  $name (exit $?)"
}

nvidia-smi --query-gpu=timestamp,power.draw,utilization.gpu,utilization.memory,memory.used,clocks.sm \
  --format=csv,noheader,nounits -l 1 > "$OUT/gpu.csv" &
SMI=$!
trap 'kill $SMI' EXIT

# A. single-stream latency
run lat_in1k_out256 --random-input-len 1024 --random-output-len 256 --num-prompts 10 --max-concurrency 1

# B. time-to-first-token vs prompt length
for L in 4096 16384 65536; do
  run ttft_in$L --random-input-len "$L" --random-output-len 32 --num-prompts 5 --max-concurrency 1
done

# C. concurrency sweep
for C in 1 8 32 64 128 256; do
  N=$(( C * 4 < 16 ? 16 : C * 4 ))
  run sweep_c$C --random-input-len 1024 --random-output-len 512 --num-prompts "$N" --max-concurrency "$C"
done
echo "results in $OUT"
