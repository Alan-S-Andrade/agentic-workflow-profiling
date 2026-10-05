#!/usr/bin/env bash
# Install Apptainer 1.4.1 (the version pinned in ghconfig.md) from source on Ubuntu 24.04 arm64.
# The 1.4.1 release has no arm64 .deb and the PPA only carries 1.5.x, hence the source build.
# Unprivileged (non-suid) install; squashfuse/fuse2fs let it mount SIF/ext3 images without root.
set -euo pipefail
VER=1.4.1 GO_VER=${GO_VER:-1.27.1}
if apptainer --version 2>/dev/null | grep -q "$VER"; then echo "apptainer $VER already installed"; exit 0; fi
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential libseccomp-dev libsubid-dev \
  pkg-config uidmap fakeroot cryptsetup tzdata dh-apparmor curl wget git \
  squashfs-tools squashfuse fuse2fs fuse3 numactl zstd >/dev/null
B=${TMPDIR:-/tmp}/apptainer-build; mkdir -p $B; cd $B
[[ -x go/bin/go ]] || curl -fsSL https://go.dev/dl/go$GO_VER.linux-arm64.tar.gz | tar xz
export PATH=$B/go/bin:$PATH
[[ -d apptainer-$VER ]] || curl -fsSL https://github.com/apptainer/apptainer/releases/download/v$VER/apptainer-$VER.tar.gz | tar xz
cd apptainer-$VER
./mconfig >/dev/null
make -s -C builddir -j"$(nproc)"
sudo make -s -C builddir install
# Ubuntu 24.04 restricts unprivileged user namespaces; allow them for apptainer's starter.
sudo tee /etc/apparmor.d/apptainer >/dev/null <<'EOF'
# Permit unprivileged user namespace creation for apptainer starter
abi <abi/4.0>,
include <tunables/global>
profile apptainer /usr/local/libexec/apptainer/bin/starter{,-suid}
    flags=(unconfined) {
  userns,
  include if exists <local/apptainer>
}
EOF
sudo systemctl reload apparmor
apptainer --version
