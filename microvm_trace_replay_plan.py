#!/usr/bin/env python3
"""Plan (and, after guest provisioning, launch) Node-0 Cloud Hypervisor replay."""
import argparse, json, os, shutil
from pathlib import Path

ROOT=Path(__file__).resolve().parent
NODE0_CPUS='0,2,4,6,8,10,12,14,16,18,20,22,24,26,28,30,32,34,36,38,40,42,44,46'
def newest(pattern):
 found=sorted((ROOT/'traces').glob(pattern),key=lambda p:p.stat().st_mtime,reverse=True)
 if not found: raise SystemExit(f'missing captured workflow: {pattern}')
 return found[0]
def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=ROOT/'microvm-replay-plan.json');p.add_argument('--sessions',type=int,default=100);p.add_argument('--kernel',type=Path,default=os.getenv('MICROVM_KERNEL'));p.add_argument('--rootfs',type=Path,default=os.getenv('MICROVM_ROOTFS'));p.add_argument('--run',action='store_true');a=p.parse_args()
 workflows=[('go-redis',newest('mini-swe-20turn-*')),('gorm',newest('external-gorm-20turn-*')),('gin',newest('external-gin-20turn-*')),('cobra',newest('external-cobra-20turn-*')),('testify',newest('external-testify-20turn-*'))]
 plan={'runtime':'cloud-hypervisor','llm_mode':'recorded-duration-wait','numa':{'cpuset_cpus':NODE0_CPUS,'memory_node':0},'memory_stop_percent':90,'guest_agent':{'transport':'vsock','port':52},'workflows':[{'name':n,'trace':str(t)} for n,t in workflows], 'tenant_sessions':[{'session':i+1,'workflow':workflows[i%len(workflows)][0]} for i in range(a.sessions)]}
 a.output.write_text(json.dumps(plan,indent=2)+'\n');print(a.output)
 if not a.run:return
 missing=[x for x in ('cloud-hypervisor','numactl') if not shutil.which(x)]
 if missing: raise SystemExit('missing required host tools: '+', '.join(missing))
 if not Path('/dev/kvm').exists(): raise SystemExit('--run requires /dev/kvm; enable KVM virtualization on the host first')
 if not a.kernel or not a.kernel.is_file() or not a.rootfs or not a.rootfs.is_file(): raise SystemExit('--run requires existing --kernel and --rootfs with replay-agent')
 raise SystemExit('guest-agent transport is intentionally not launched until its vsock protocol is provisioned and validated')
if __name__=='__main__':main()
