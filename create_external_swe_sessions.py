#!/usr/bin/env python3
"""Create four 20-turn historical-feature SWE-agent manifests from Git history."""
import json, subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parent
PROJECTS={'gorm':'go-gorm/gorm','gin':'gin-gonic/gin','cobra':'spf13/cobra','testify':'stretchr/testify'}
PROMPT='''You are maintaining {project}. Reimplement one historical resolved change as a complete production coding episode.\n\nHistorical change: {title}\nReference commit: {commit}\n\nDo not inspect, cherry-pick, or copy the reference commit. Infer the intended behavior from the current code, issue-style title, APIs, and tests. Inspect first; implement the smallest compatible change; write focused tests; compile; run targeted and broader tests; diagnose failures; add regression tests; update docs or GoDoc when warranted; and finish with git diff --check. Do not commit. This entire workflow is one semantic turn.\n'''
def git(repo,*args): return subprocess.check_output(['git','-C',str(repo),*args],text=True)
def main():
 out=ROOT/'sessions';out.mkdir(exist_ok=True)
 for name,project in PROJECTS.items():
  repo=ROOT/'targets'/name
  raw=git(repo,'log','--all','--format=%H%x09%s','--regexp-ignore-case','--extended-regexp','--grep=^(feat|fix)','-40').splitlines()
  turns=[]
  for n,line in enumerate(raw[:20],1):
   commit,title=line.split('\t',1); base=git(repo,'rev-parse',commit+'^').strip()
   turns.append({'turn':n,'commit':commit,'base_commit':base,'title':title,'prompt':PROMPT.format(project=project,title=title,commit=commit),'inference_budget':14})
  if len(turns)!=20: raise SystemExit(f'{name}: insufficient historical changes')
  target=out/name;target.mkdir(exist_ok=True)
  (target/'session.json').write_text(json.dumps({'project':project,'model':'gpt-5.6-luna','reasoning_effort':'medium','turns':turns},indent=2)+'\n')
  with (target/'turns.jsonl').open('w') as f:
   for t in turns:f.write(json.dumps(t)+'\n')
  print(target/'session.json')
if __name__=='__main__':main()
