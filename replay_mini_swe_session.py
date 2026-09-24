#!/usr/bin/env python3
"""No-LLM replay of a captured multi-turn SWE-agent session in one container."""
import argparse, json, os, shutil, subprocess, time
from pathlib import Path

def rows(path): return [json.loads(x) for x in path.read_text().splitlines() if x]

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('session',type=Path); ap.add_argument('--workspace',type=Path,required=True); ap.add_argument('--output',type=Path,required=True); ap.add_argument('--image',default='openhands-trace-replay:go1.24'); ap.add_argument('--label'); ap.add_argument('--cpus',required=True); ap.add_argument('--mems',default='0'); ap.add_argument('--speed',type=float,default=1.0); ap.add_argument('--loop',action='store_true'); ap.add_argument('--idle-hold',type=float,default=86400); args=ap.parse_args()
    manifest=rows(args.session/'turns.jsonl'); steps=[]
    for episode in manifest:
        trace=Path(episode['trace'])
        # Session manifests record the capture host's absolute trace path.
        # Resolve by basename after a portable clone on another machine.
        if not trace.is_dir(): trace=Path(__file__).resolve().parent/'traces'/trace.name
        llm=rows(trace/'llm.jsonl'); events=rows(trace/'execution.jsonl')
        for x in llm: steps.append((x['started_at'],{'kind':'llm_wait','turn':episode['turn'],'duration_ms':x['duration_ms']}))
        for x in events:
            if x.get('event')=='start' and x.get('node_type')=='tool' and x.get('command') is not None:
                steps.append((x['started_at'],{'kind':'tool','turn':episode['turn'],'command':x['command']}))
    steps.sort(key=lambda x:x[0]); args.output.mkdir(parents=True)
    tool_root=Path('/tmp/swe-agent-tools')
    docker=['sudo','docker']; cmd=docker+['run','-d','--rm','--init','--network','none','--workdir','/workspace','--cpuset-cpus',args.cpus,'--cpuset-mems',args.mems,'--mount',f'type=bind,src={args.workspace.resolve()},dst=/workspace']
    if tool_root.is_dir(): cmd += ['--mount',f'type=bind,src={tool_root},dst=/tmp/swe-agent-tools,readonly']
    if args.label: cmd += ['--label',args.label]
    cmd += [args.image,'bash','-lc','exec sleep infinity']; c=subprocess.run(cmd,text=True,capture_output=True,check=True).stdout.strip()
    try:
      with (args.output/'steps.jsonl').open('w') as f:
       it=0
       while True:
        it+=1
        for n,(_,step) in enumerate(steps,1):
          r={**step,'order':n,'iteration':it,'started_at':time.time()}
          if step['kind']=='llm_wait': time.sleep(step['duration_ms']/1000*args.speed); r['mocked']=True
          else:
            command=step['command'].replace('/users/alanuiuc/agentic-workflow-profiling/targets/mini-swe-session-target','/workspace').replace('/users/alanuiuc/agentic-workflow-profiling/swe-default-target','/workspace')
            path='/tmp/swe-agent-tools/registry/bin:/tmp/swe-agent-tools/edit_anthropic/bin:/tmp/swe-agent-tools/review_on_submit_m/bin:/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin'
            q=subprocess.run(docker+['exec','--workdir','/workspace','--env',f'PATH={path}',c,'bash','-c',command],text=True,capture_output=True)
            r.update({'exit_status':q.returncode,'stdout':q.stdout,'stderr':q.stderr})
          r['ended_at']=time.time(); r['duration_ms']=(r['ended_at']-r['started_at'])*1000; f.write(json.dumps(r)+'\n'); f.flush()
        if not args.loop: break
      time.sleep(args.idle_hold)
    finally: subprocess.run(docker+['rm','-f',c],capture_output=True)
if __name__=='__main__': main()
