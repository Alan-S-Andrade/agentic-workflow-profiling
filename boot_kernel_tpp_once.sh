#!/usr/bin/env bash
# Validate this host's installed TPP kernel; optionally select it for one boot.
set -euo pipefail
case "${1:---check}" in
  --check|--arm) action="${1:---check}" ;;
  *) echo "Usage: sudo bash $0 [--check|--arm]" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || exit 2
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }

release=5.15.0-tpp
fallback=5.15.0-187-generic
uuid=571337a8-0ea6-44bd-9cfc-6cb9719427c1
submenu="gnulinux-advanced-$uuid"
entry="gnulinux-$release-advanced-$uuid"
stock="gnulinux-$fallback-advanced-$uuid"
manifest=/boot/tpp-backup-20261019/boot-files.sha256

[[ $(findmnt -nro UUID /) == "$uuid" ]] || { echo 'Unexpected root filesystem.' >&2; exit 1; }
[[ ! -d /sys/firmware/efi ]] || { echo 'This procedure was validated for BIOS boot.' >&2; exit 1; }
[[ -s $manifest ]] || { echo "Missing installation checksums: $manifest" >&2; exit 1; }
sha256sum --check "$manifest"
grub-script-check /boot/grub/grub.cfg
grep -Fq "'$entry'" /boot/grub/grub.cfg
grep -Fq "'$stock'" /boot/grub/grub.cfg
grep -Fq "set default=\"$submenu>$stock\"" /boot/grub/grub.cfg
for module in mpt3sas ahci ixgbe i40e ipod; do
  [[ $(modinfo -k "$release" -F vermagic "$module") == "$release "* ]]
done
listing=$(lsinitramfs "/boot/initrd.img-$release")
for module in mpt3sas raid_class scsi_transport_sas ahci libahci; do
  grep -Eq "/$module\\.ko(\\.(xz|zst|gz))?$" <<< "$listing"
done
grep -q '^CONFIG_EXT4_FS=y$' "/boot/config-$release"
grep -q '^CONFIG_SERIAL_8250_CONSOLE=y$' "/boot/config-$release"
grep -q '^CONFIG_NUMA_BALANCING=y$' "/boot/config-$release"
grep -q '^CONFIG_KVM_INTEL=y$' "/boot/config-$release"
echo "Boot files, root UUID, modules, initramfs, GRUB syntax and stock default verified."
if [[ $action == --arm ]]; then
  grub-reboot "$submenu>$entry"
  grub-editenv /boot/grub/grubenv list | grep -Fx "next_entry=$submenu>$entry"
  sync
  echo "TPP selected for ONE boot. Open the CloudLab serial console, then run: sudo reboot"
else
  echo "No boot selection changed. Use --arm to select TPP for one boot."
  grub-editenv /boot/grub/grubenv list
fi
