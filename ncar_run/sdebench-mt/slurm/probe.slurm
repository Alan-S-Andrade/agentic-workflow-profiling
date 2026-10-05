#!/bin/bash
#SBATCH -J sdebench-probe
#SBATCH -p gh-dev
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -c 72
#SBATCH -t 01:00:00
#SBATCH -A ASC26114
#SBATCH -o probe_%j.out
# Step 1: verify KVM + Cloud Hypervisor on a GH200 compute node, build the base
# guest image with apptainer --fakeroot, boot one VM and do a vsock round trip.
set -o pipefail
MT=$WORK/sdebench-mt
cd $MT/vm
echo "== host"; hostname; nproc; lscpu | grep -E "Model name|NUMA node"; numactl -H | head -12; free -g; df -h /tmp
echo "== kvm"; ls -l /dev/kvm; id; test -r /dev/kvm -a -w /dev/kvm && echo KVM_RW=yes || echo KVM_RW=no
which numactl taskset mkfs.ext4; mkfs.ext4 -V 2>&1 | head -1
grep -iE "OVERLAY_FS|VIRTIO_VSOCK|VHOST_VSOCK" /boot/config-$(uname -r) 2>/dev/null

module load tacc-apptainer
apptainer --version
export APPTAINER_CACHEDIR=$MT/containers/cache APPTAINER_TMPDIR=/tmp/$USER-apt
mkdir -p $APPTAINER_TMPDIR
echo "== build base (fakeroot)"
SB=/tmp/$USER-base-sb
rm -rf $SB
if apptainer build --fakeroot --sandbox $SB base.def > build_base.log 2>&1; then
  echo BASE_BUILD=ok
else
  echo BASE_BUILD=failed; tail -30 build_base.log; exit 1
fi
bash sandbox_to_ext4.sh $SB images/base.ext4 2

echo "== boot test"
cd $MT
python3 - <<'EOF'
import sys, time
sys.path.insert(0, ".")
from sdebench_mt.vmm import MicroVM
vm = MicroVM("probe", "vm/images/base.ext4", "/tmp/" + __import__("os").environ["USER"] + "-vms", vcpus=2, mem_mb=2048)
try:
    vm.start()
    print(f"BOOT_OK boot_s={vm.boot_s:.2f}")
    for c in ["uname -a", "cat /proc/mounts | grep -E ' / |overlay'", "nproc; free -m",
              "echo hello > /root/x && cat /root/x", "git --version; /opt/miniconda3/bin/conda --version; python3 --version"]:
        r = vm.exec(c, cwd="/")
        print(f"$ {c}\n[{r['exit_status']}] {r['stdout']}{r['stderr']}")
    t = time.time(); n = 50
    for _ in range(n): vm.exec("true", cwd="/")
    print(f"exec round-trip mean {(time.time()-t)/n*1000:.1f} ms")
except Exception as e:
    print("BOOT_FAIL", e)
    print(vm.tail(4000))
finally:
    vm.stop(keep_dir=False)
EOF

echo "== real SWE-bench instance: build + gold-patch eval"
export MT TMPDIR=/tmp/$USER-build; mkdir -p $TMPDIR
IID=psf__requests-2317
HF_HOME=$MT/models env/venv/bin/python -c "
import json; from datasets import load_dataset
ds={x['instance_id']:x for x in load_dataset('SWE-bench/SWE-bench_Verified',split='test')}
open('configs/probe_instance.jsonl','w').write(json.dumps(ds['$IID'])+'\n')" 2>/dev/null
rm -f vm/images/$IID.ext4
bash vm/build_instance.sh $IID && echo IMG_OK || { echo IMG_FAIL; tail -20 vm/images/$IID.build.log; }
env/venv/bin/python sdebench_mt/gold_check.py $IID --instances configs/probe_instance.jsonl --out runs/probe_gold.jsonl
