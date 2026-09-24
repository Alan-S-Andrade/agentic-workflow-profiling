#!/usr/bin/env python3
"""Node-0 memory-capacity experiment using recorded 20-turn SWE-agent traces."""
import argparse,json,os,re,shutil,subprocess,time,uuid
from pathlib import Path
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
G=2**30; CPUS='0,2,4,6,8,10,12,14,16,18,20,22,24,26,28,30,32,34,36,38,40,42,44,46'
def run(x): return subprocess.run(x,text=True,capture_output=True)
def node0():
 for x in Path('/sys/devices/system/node/node0/meminfo').read_text().splitlines():
  m=re.search(r'MemTotal:\s+(\d+)',x)
  if m:return int(m.group(1))*1024
def tick():
 want={'cpu'+x for x in CPUS.split(',')}; t=b=0
 for x in Path('/proc/stat').read_text().splitlines():
  z=x.split()
  if z and z[0] in want: v=list(map(int,z[1:]));t+=sum(v);b+=sum(v[:3])
 return t,b
def containers(label): return run(['sudo','docker','ps','-q','--filter',f'label={label}']).stdout.split()
def memory(ids):
 if not ids:return 0
 total=0
 for line in run(['sudo','docker','stats','--no-stream','--format','{{.MemUsage}}',*ids]).stdout.splitlines():
  m=re.match(r'([0-9.]+)([KMG]iB)',line)
  if m: total+=float(m.group(1))*{'KiB':1024,'MiB':1024**2,'GiB':1024**3}[m.group(2)]
 return int(total)
def main():
 p=argparse.ArgumentParser();p.add_argument('session',type=Path);p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--max-sessions',type=int,default=512);p.add_argument('--batch',type=int,default=4);p.add_argument('--speed',type=float,default=1);a=p.parse_args()
 if os.geteuid()!=0: raise SystemExit('run as root');
 a.output.mkdir(); label='mini-swe.node0='+uuid.uuid4().hex; cap=node0();limit=cap*.9; procs=[];rows=[]
 try:
  for first in range(1,a.max_sessions+1,a.batch):
   for i in range(first,min(first+a.batch,a.max_sessions+1)):
    ws=a.output/f'workspace-{i:04d}';subprocess.run(['cp','-a','--reflink=always',str(a.source),str(ws)],check=True);out=a.output/f'replay-{i:04d}'
    f=(a.output/f'replay-{i:04d}.log').open('w'); q=subprocess.Popen(['python3',str(Path(__file__).with_name('replay_mini_swe_session.py')),str(a.session),'--workspace',str(ws),'--output',str(out),'--label',label,'--cpus',CPUS,'--mems','0','--speed',str(a.speed),'--loop'],stdout=f,stderr=subprocess.STDOUT);procs.append((q,f,ws))
   time.sleep(5); ids=containers(label); before=tick();time.sleep(1);after=tick(); mem=memory(ids);row={'resident_sessions':len(ids),'docker_cgroup_memory_bytes':mem,'node0_cpu_percent':(after[1]-before[1])/(after[0]-before[0])*100 if after[0]>before[0] else 0};rows.append(row);(a.output/'samples.jsonl').open('a').write(json.dumps(row)+'\n');print(json.dumps(row),flush=True)
   if mem>=limit:break
 finally:
  for q,f,w in procs:q.terminate();f.close()
  ids=containers(label)
  if ids:run(['sudo','docker','rm','-f',*ids])
  for _,_,w in procs:shutil.rmtree(w,ignore_errors=True)
 with (a.output/'samples.jsonl').open('w') as f:
  for r in rows:f.write(json.dumps(r)+'\n')
 fig,ax=plt.subplots(figsize=(8,4.8));r=ax.twinx();x=[z['resident_sessions'] for z in rows];ax.plot(x,[z['docker_cgroup_memory_bytes']/G for z in rows],color='#2457a6');r.plot(x,[z['node0_cpu_percent'] for z in rows],color='#d05a32');ax.set_xlabel('Resident sessions');ax.set_ylabel('Docker cgroup memory (GiB)');r.set_ylabel('Node-0 CPU (%)');ax.set_title('Node-0 20-turn SWE trace replay');ax.grid(alpha=.25);fig.tight_layout();fig.savefig(a.output/'node0-mini-swe-replay-capacity.png',dpi=200)
if __name__=='__main__':main()
