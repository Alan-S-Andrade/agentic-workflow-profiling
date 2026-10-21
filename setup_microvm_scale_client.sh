#!/usr/bin/env bash
# Provision a Linux host to run swebench_microvm_scale_experiment.py.
# Run as root: sudo ./setup_microvm_scale_client.sh [inference-server-ip] [inference-server-port]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_IP="${1:-128.110.220.114}"
SERVER_PORT="${2:-8080}"
CH_VERSION="v53.0"
ASSETS="$ROOT/microvm-assets"
ROOTFS_TREE="$ASSETS/rootfs-tree"
ROOTFS_IMAGE="$ASSETS/replay-rootfs.img"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,3p' "$0"
  echo "Default inference server: 128.110.220.114:8080"
  exit 0
fi
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo $0 [inference-server-ip] [inference-server-port]" >&2
  exit 1
fi
if [[ "$#" -gt 2 ]]; then
  echo "Usage: sudo $0 [inference-server-ip] [inference-server-port]" >&2
  exit 2
fi
python3 - "$SERVER_IP" <<'PY'
import ipaddress, sys
try:
    ipaddress.IPv4Address(sys.argv[1])
except ValueError:
    raise SystemExit("server address must be an IPv4 address")
PY
if ! [[ "$SERVER_PORT" =~ ^[0-9]+$ ]] || (( SERVER_PORT < 1 || SERVER_PORT > 65535 )); then
  echo "server port must be between 1 and 65535" >&2
  exit 2
fi
cd "$ROOT"
if [[ "$(uname -s)" != Linux ]] || ! command -v apt-get >/dev/null; then
  echo "This setup script requires an Ubuntu or Debian Linux host." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  busybox-static ca-certificates curl debootstrap e2fsprogs iproute2 iptables \
  numactl python3

case "$(uname -m)" in
  x86_64|amd64)
    DEB_ARCH=amd64
    CH_ASSET=cloud-hypervisor-static
    ;;
  aarch64|arm64)
    DEB_ARCH=arm64
    CH_ASSET=cloud-hypervisor-static-aarch64
    ;;
  *)
    echo "Unsupported host architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

mkdir -p "$ROOT/.tools" "$ASSETS"
CH_BIN="$ROOT/.tools/cloud-hypervisor"
if [[ ! -x "$CH_BIN" ]] || ! "$CH_BIN" --version 2>/dev/null | grep -q "cloud-hypervisor $CH_VERSION"; then
  temporary="$CH_BIN.tmp.$$"
  trap 'rm -f "$temporary"' EXIT
  curl -fL --retry 3 \
    "https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/$CH_VERSION/$CH_ASSET" \
    -o "$temporary"
  chmod 0755 "$temporary"
  mv -f "$temporary" "$CH_BIN"
  trap - EXIT
fi

KERNEL="/boot/vmlinuz-$(uname -r)"
INITRAMFS="/boot/initrd.img-$(uname -r)"
if [[ ! -s "$KERNEL" || ! -s "$INITRAMFS" ]]; then
  echo "Missing host kernel or initramfs for $(uname -r) under /boot." >&2
  echo "Install a boot kernel and initramfs, then rerun setup." >&2
  exit 1
fi
if [[ "$DEB_ARCH" == arm64 ]] && gzip -t "$KERNEL" >/dev/null 2>&1; then
  gzip -dc "$KERNEL" > "$ASSETS/vmlinuz"
  chmod 0644 "$ASSETS/vmlinuz"
else
  install -m 0644 "$KERNEL" "$ASSETS/vmlinuz"
fi

if [[ ! -d "$ROOTFS_TREE" ]]; then
  case "$DEB_ARCH" in
    amd64) UBUNTU_MIRROR=https://archive.ubuntu.com/ubuntu ;;
    arm64) UBUNTU_MIRROR=https://ports.ubuntu.com/ubuntu-ports ;;
  esac
  debootstrap --arch="$DEB_ARCH" --variant=minbase jammy "$ROOTFS_TREE" "$UBUNTU_MIRROR"
  chroot "$ROOTFS_TREE" apt-get update
  chroot "$ROOTFS_TREE" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bash busybox-static ca-certificates iproute2 kmod python3
  chroot "$ROOTFS_TREE" apt-get clean
  rm -rf "$ROOTFS_TREE/var/lib/apt/lists/"*
fi
if ! chroot "$ROOTFS_TREE" /usr/bin/python3 -c 'import tarfile' >/dev/null 2>&1 || \
   ! chroot "$ROOTFS_TREE" test -x /sbin/modprobe; then
  chmod 1777 "$ROOTFS_TREE/tmp"
  chroot "$ROOTFS_TREE" apt-get update
  chroot "$ROOTFS_TREE" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends kmod python3
  chroot "$ROOTFS_TREE" apt-get clean
  rm -rf "$ROOTFS_TREE/var/lib/apt/lists/"*
fi
HOST_MODULES="/lib/modules/$(uname -r)"
GUEST_MODULES="$ROOTFS_TREE/lib/modules/$(uname -r)"
if [[ -d "$HOST_MODULES" ]] && command -v depmod >/dev/null; then
  for module in vsock.ko.zst vmw_vsock_virtio_transport_common.ko.zst vmw_vsock_virtio_transport.ko.zst; do
    source_module="$(find "$HOST_MODULES" -type f -name "$module" -print -quit)"
    if [[ -n "$source_module" ]]; then
      install -D -m 0644 "$source_module" "$GUEST_MODULES/kernel/net/vmw_vsock/$module"
    fi
  done
  depmod -b "$ROOTFS_TREE" "$(uname -r)"
fi
for required in usr/bin/python3 usr/bin/busybox bin/bash sbin/ip; do
  if [[ ! -e "$ROOTFS_TREE/$required" ]]; then
    echo "Guest root filesystem is incomplete: $ROOTFS_TREE/$required is missing." >&2
    echo "Repair or remove $ROOTFS_TREE and rerun setup." >&2
    exit 1
  fi
done

if [[ ! -s "$ROOTFS_IMAGE" ]]; then
  truncate -s 2G "$ROOTFS_IMAGE"
  mkfs.ext4 -F -q -d "$ROOTFS_TREE" "$ROOTFS_IMAGE"
fi

# The experiment requires swap disabled so its memory guard reflects RAM use.
swapoff -a

cat > "$ROOT/cloud-hypervisor.env" <<ENV
# Source before invoking a microVM runner from this checkout.
MICROVM_REPO_ROOT="$ROOT"
export PATH="\$MICROVM_REPO_ROOT/.tools:\$PATH"
export CLOUD_HYPERVISOR_BIN="\$MICROVM_REPO_ROOT/.tools/cloud-hypervisor"
export MICROVM_KERNEL="\$MICROVM_REPO_ROOT/microvm-assets/vmlinuz"
export MICROVM_ROOTFS="\$MICROVM_REPO_ROOT/microvm-assets/replay-rootfs.img"
export MICROVM_INFERENCE_ENDPOINT="http://$SERVER_IP:$SERVER_PORT/v1/inference"
export MICROVM_INITRAMFS="$INITRAMFS"
unset MICROVM_CPUSET MICROVM_NUMA_NODE MICROVM_NUMA_NODES
ENV
chmod 0644 "$ROOT/cloud-hypervisor.env"

if [[ ! -c /dev/kvm || ! -r /dev/kvm || ! -w /dev/kvm ]]; then
  echo "Setup completed, but /dev/kvm is unavailable to root; enable KVM before running." >&2
  exit 1
fi

if curl -fsS --max-time 5 "http://$SERVER_IP:$SERVER_PORT/healthz" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("Server health:", "ok" if d.get("ok") else "unhealthy", "instances:", d.get("instances", "unknown"))'; then
  :
else
  echo "Warning: inference server health check failed; verify it before running." >&2
fi

if [[ "$DEB_ARCH" == arm64 ]]; then
  RUN_MODE="--arch arm64 --slurm-vsock"
else
  RUN_MODE=""
fi

cat <<EOF2
MicroVM scale client setup is ready.
Endpoint: http://$SERVER_IP:$SERVER_PORT/v1/inference
Cloud Hypervisor: $CH_BIN
Kernel: $ASSETS/vmlinuz
Guest initramfs: $INITRAMFS

Launch the default 1/2/4/8/16/32/64/128 sweep with:
  cd "$ROOT"
  source "$ROOT/cloud-hypervisor.env"
  sudo -n env PATH="\$PATH" MICROVM_INFERENCE_ENDPOINT="\$MICROVM_INFERENCE_ENDPOINT" \\
    python3 "$ROOT/swebench_microvm_scale_experiment.py" \\
    $RUN_MODE --output "traces/microvm-scale-\$(date -u +%Y%m%dT%H%M%SZ)" --run

Swap is disabled until reboot. Run this setup script again after reboot before starting a run.
EOF2
