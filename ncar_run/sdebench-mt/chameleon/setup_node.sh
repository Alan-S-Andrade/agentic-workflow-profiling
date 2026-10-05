#!/usr/bin/env bash
# One-time setup per lease (idempotent): apptainer, Python env, Cloud Hypervisor + guest kernel,
# model link. Afterwards restore the benchmark data with `chameleon/store.sh pull`, or build it
# from scratch with `chameleon/build_images.sh`.
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd $MT

echo "== apptainer"
bash chameleon/install_apptainer.sh

echo "== python env (ghconfig.md §3, §9.1)"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/0.12.22/install.sh | sh
[[ -x env/venv/bin/python ]] || uv venv -q -p 3.12.15 env/venv
VIRTUAL_ENV=env/venv uv pip install -q mini-swe-agent==2.4.6 swebench==5.0.2 litellm==1.103.2 \
  datasets==5.0.1 "huggingface_hub[cli]==1.33.0" httpx==0.28.1 matplotlib==3.11.2 \
  openai==2.54.0 pyyaml==6.0.3 numpy==2.5.3 pandas aiohttp
env/venv/bin/python --version

echo "== Cloud Hypervisor v53.0 + guest kernel (sha256 from ghconfig.md §3)"
mkdir -p vm/bin vm/kernel vm/images containers runs
R=https://github.com/cloud-hypervisor
[[ -s vm/bin/cloud-hypervisor ]] || curl -fsSL -o vm/bin/cloud-hypervisor $R/cloud-hypervisor/releases/download/v53.0/cloud-hypervisor-static-aarch64
[[ -s vm/bin/ch-remote ]] || curl -fsSL -o vm/bin/ch-remote $R/cloud-hypervisor/releases/download/v53.0/ch-remote-static-aarch64
[[ -s vm/kernel/Image ]] || curl -fsSL -o vm/kernel/Image $R/linux/releases/download/ch-release-v6.16.9-20260508/Image-arm64
chmod +x vm/bin/*
sha256sum -c <<'EOF'
f192b510eea1c710cbc439d716bb0573c223fc463dbe3e6523788a2b7ef62850  vm/bin/cloud-hypervisor
69d1b1235381ec50f1b45cf771a7dff4a9013d452833ab34682d6283e2114010  vm/kernel/Image
EOF

echo "== model (serve_vllm.sh binds \$MT/models:/models)"
[[ -e models ]] || ln -s $HOME/models models
if [[ -s models/Qwen3.8-27B/model-00018-of-00018.safetensors ]]; then
  echo "Qwen3.8-27B present"
else
  echo "Qwen3.8-27B missing: run ~/bin/weights.sh pull (needs ~/.config/rclone/rclone.conf)"
fi

echo "== host checks"
test -r /dev/kvm -a -w /dev/kvm && echo "KVM_RW=yes" || echo "KVM_RW=no (fix /dev/kvm access)"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
