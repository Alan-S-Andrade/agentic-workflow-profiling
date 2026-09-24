#!/usr/bin/env python3
"""Plot per-semantic-turn PSS and CPU from a completed captured session."""
import json, sys
from pathlib import Path
import matplotlib.pyplot as plt

HZ=100
def metrics(trace):
    samples=[json.loads(x) for x in (Path(trace)/'process-samples.jsonl').read_text().splitlines() if x]
    by={}
    for s in samples:
        if s.get('event') == 'sample':
            by.setdefault(s['observed_at'], []).append(s)
    ts=sorted(by); peak=max(sum((s.get('pss_bytes') or 0) for s in by[t]) for t in ts)/1024**2
    values=[]
    for a,b in zip(ts,ts[1:]):
        prev={s['pid']:s for s in by[a]}; elapsed=b-a; ticks=0
        for s in by[b]:
            p=prev.get(s['pid'])
            if p:
                ticks += max(0, (s.get('cpu_ms') or 0) - (p.get('cpu_ms') or 0))
        values.append(100*(ticks/1000)/elapsed)
    return peak, sum(values)/len(values) if values else 0
def main(session):
    d=Path(session); rows=[json.loads(x) for x in (d/'turns.jsonl').read_text().splitlines() if x]
    out=[]
    for r in rows:
        if r.get('trace') and Path(r['trace']).joinpath('process-samples.jsonl').exists():
            p,c=metrics(r['trace']); out.append({**r,'peak_pss_mb':p,'mean_cpu_percent':c,'resident_sessions':1})
    (d/'resource_summary.json').write_text(json.dumps(out,indent=2)+'\n')
    x=[r['turn'] for r in out]; fig,ax=plt.subplots(figsize=(8,4.8)); ax2=ax.twinx()
    ax.plot(x,[r['peak_pss_mb'] for r in out],marker='o',color='#2457a6',label='PSS')
    ax2.plot(x,[r['mean_cpu_percent'] for r in out],marker='o',color='#d05a32',label='CPU')
    ax.set_xlabel('Semantic turn (resident sessions = 1)'); ax.set_ylabel('Peak PSS (MB)'); ax2.set_ylabel('Mean CPU (%)'); ax.set_title('20-turn mini-SWE-agent session')
    ax.grid(alpha=.25); fig.tight_layout(); fig.savefig(d/'sessions_cpu_memory.png',dpi=180); print(d/'sessions_cpu_memory.png')
if __name__=='__main__': main(sys.argv[1])
