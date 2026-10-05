# Sourced by chameleon/*.sh. Maps the Vista layout ($WORK/sdebench-mt, `module`, Slurm) onto a
# Chameleon CHI@NCAR GH200 node so the original slurm/*.sh helpers run unchanged.
export WORK=$HOME MT=$HOME/sdebench-mt
export USER=${USER:-$(id -un)}
# chameleon/bin provides a no-op `module`; uv lives in ~/.local/bin; apptainer in /usr/local/bin.
export PATH=$MT/chameleon/bin:$HOME/.local/bin:/usr/local/bin:$PATH
# CPU KV-cache offload arm ("KV-cache migration"): GiB of pinned Grace memory for vLLM's CPU KV cache.
export OFFLOAD_GIB=${OFFLOAD_GIB:-64}
# vLLM needs ~13 min to become healthy here (slow safetensors load + squashfuse-mounted SIF);
# serve_vllm.sh's default 900 s health wait is too tight.
export HEALTH_TIMEOUT_S=${HEALTH_TIMEOUT_S:-2400}
# GPU KV cache pinned for both arms: vLLM's profile-based sizing varied 26.63-28.38 GiB
# (809,733-863,618 tokens) between starts here; 26.7 GiB ~ 812K tokens, mid Vista range. 0 = unpinned.
export KV_CACHE_BYTES=${KV_CACHE_BYTES:-28668876800}

# Weight-offload arm: GiB of weights moved to pinned Grace memory and read in place over NVLink-C2C
# (vLLM UVA offloader, --cpu-offload-gb); the GPU KV pin grows by the same amount.
export UVA_GIB=${UVA_GIB:-8}

# Extra vLLM flags per arm, appended by slurm/serve_vllm.sh via EXTRA_VLLM_ARGS.
#   baseline: GPU KV only            offload: + native CPU KV cache ($OFFLOAD_GIB GiB)
#   uva: $UVA_GIB GiB of weights in Grace memory, GPU KV = KV_CACHE_BYTES + $UVA_GIB GiB, no KV offload
arm_args() {
  local kv=$KV_CACHE_BYTES extra=""
  case $1 in
    baseline) ;;
    offload)  extra="--kv-offloading-backend native --kv-offloading-size $OFFLOAD_GIB" ;;
    uva)      extra="--cpu-offload-gb $UVA_GIB"; (( kv > 0 )) && kv=$(( kv + UVA_GIB * 1073741824 )) ;;
    *) echo "unknown arm $1" >&2; return 2 ;;
  esac
  (( kv > 0 )) && echo "--kv-cache-memory-bytes $kv $extra" || echo "$extra"
}
