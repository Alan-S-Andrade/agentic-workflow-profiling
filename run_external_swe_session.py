#!/usr/bin/env python3
"""Sequentially capture one 20-turn external historical SWE-agent session."""
import json,os,subprocess,sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def main():
 if len(sys.argv)!=3: raise SystemExit('usage: run_external_swe_session.py SESSION_DIR TARGET')
 s=Path(sys.argv[1]);target=Path(sys.argv[2]); turns=[json.loads(x) for x in (s/'turns.jsonl').read_text().splitlines() if x]
 out=ROOT/'traces'/f'external-{s.name}-20turn-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}';out.mkdir()
 for t in turns:
  subprocess.run(['git','-C',str(target),'checkout','--detach','--force',t['base_commit']],check=True,stdout=subprocess.DEVNULL)
  subprocess.run(['git','-C',str(target),'clean','-ffd'],check=True,stdout=subprocess.DEVNULL)
  env=os.environ|{'SWE_TARGET':str(target)}
  p=subprocess.run([str(ROOT/'profile-run.sh'),'swe-agent',t['prompt']],cwd=ROOT,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
  trace=p.stdout.strip().splitlines()[-1] if p.stdout.strip() else None
  r={'turn':t['turn'],'title':t['title'],'commit':t['commit'],'base_commit':t['base_commit'],'trace':trace,'exit_status':p.returncode}
  with (out/'turns.jsonl').open('a') as f:f.write(json.dumps(r)+'\n')
  print(json.dumps(r),flush=True)
  if p.returncode: raise SystemExit(p.returncode)
 print(out)
if __name__=='__main__':main()
