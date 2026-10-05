#!/usr/bin/env bash
# Usage: build_instance.sh <instance_id>
# Converts the official swebench arm64 eval image into a Cloud Hypervisor rootfs:
# apptainer sandbox (no root needed: no %post) + replay-agent + overlay init -> ext4.
set -euo pipefail
MT=${MT:-$WORK/sdebench-mt}
iid=$1
out=$MT/vm/images/$iid.ext4
[[ -s $out ]] && { echo "skip $iid (exists)"; exit 0; }
name=$(echo "sweb.eval.arm64.${iid//__/_1776_}" | tr A-Z a-z)
# Pin to the digest recorded in configs/image_digests.txt when present (ghconfig.md §7), else :latest.
ref=$(awk -v i="$iid" '$1 == i {print "@" $2}' "$MT/configs/image_digests.txt" 2>/dev/null)
sb=${TMPDIR:-/tmp}/sb-$iid
rm -rf "$sb"
# Concurrent builds sharing one cache hang at "Fetching OCI image" (ghconfig.md §6, §11).
export APPTAINER_CACHEDIR=${TMPDIR:-/tmp}/apcache-$iid
apptainer build -F --sandbox "$sb" "docker://swebench/$name${ref:-:latest}" > "$MT/vm/images/$iid.build.log" 2>&1
chmod -R u+rwX "$sb" 2>/dev/null || true
install -m 755 "$MT/vm/guest/replay-agent" "$sb/usr/local/bin/replay-agent"
install -D -m 755 "$MT/vm/guest/init" "$sb/sbin/replay-init"
py=/opt/miniconda3/bin/python3; [[ -x $sb$py ]] || py=/usr/bin/python3
sed -i "1s|.*|#!$py|" "$sb/usr/local/bin/replay-agent"
# Files are owned by the build uid inside the guest; let git (run as root) use them.
printf '[safe]\n\tdirectory = *\n' >> "$sb/etc/gitconfig"
mkdir -p "$sb"/{mnt/upper,mnt/lower,mnt/root,proc,sys,dev,tmp,run,root}
bash "$MT/vm/sandbox_to_ext4.sh" "$sb" "$out" 3
rm -rf "$sb"
