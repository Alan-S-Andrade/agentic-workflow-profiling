#!/usr/bin/env bash
# Usage: sandbox_to_ext4.sh <apptainer-sandbox-dir> <out.ext4> [extra_gb]
# Packs a sandbox directory into an ext4 image unprivileged (mkfs.ext4 -d).
# Block size is forced to 4 KiB: the host has 64 KiB pages (mke2fs would default
# to 64 KiB blocks) but the guest kernel uses 4 KiB pages. mke2fs must run on
# node-local disk because on Lustre it takes the 4 MiB stripe as the device's
# sector size and refuses 4 KiB blocks; the result is then copied sparsely.
set -euo pipefail
src=$1 out=$2 extra=${3:-2}
# Unprivileged extraction makes every file owned by the build user; setuid bits
# would then drop the guest's PID 1 (root) to that uid (e.g. util-linux mount).
find "$src" -xdev -type f -perm /6000 -exec chmod ug-s {} + 2>/dev/null || true
used_mb=$(du -sm --apparent-size "$src" | cut -f1)
size_mb=$(( used_mb * 13 / 10 + extra * 1024 ))
tmp=$(mktemp -p "${TMPDIR:-/tmp}" rootfs.XXXXXX.ext4)
trap 'rm -f "$tmp"' EXIT
truncate -s "${size_mb}M" "$tmp"
mkfs.ext4 -q -F -b 4096 -L rootfs -N $(( size_mb * 64 )) -d "$src" "$tmp"
mkdir -p "$(dirname "$out")"
cp --sparse=always "$tmp" "$out.partial" && mv -f "$out.partial" "$out"
echo "$out: ${size_mb} MiB (content ${used_mb} MiB)"
