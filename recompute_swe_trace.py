#!/usr/bin/env python3
"""Materialize per-turn replay records and a resource plot from one SWE trace."""
import json, sys
from pathlib import Path
import matplotlib.pyplot as plt

def main(run_dir):
    d=Path(run_dir); llm=[json.loads(x) for x in (d/'llm.jsonl').read_text().splitlines() if x.strip()]
    turns=[]
    for i,e in enumerate(llm,1):
        r=e.get('response') or {}; choices=r.get('choices') or []; msg=(choices[0].get('message') or {}) if choices else {}
        calls=msg.get('tool_calls') or []
        tools=[]
        for c in calls:
            fn=c.get('function') or {}; tools.append(fn.get('name') or c.get('type'))
        turns.append({'turn':i,'started_at':e.get('started_at'),'inference_ms':e.get('duration_ms'),
                      'model':e.get('model'),'reasoning_effort':(e.get('request') or {}).get('reasoning_effort'),
                      'tool_calls':tools,'request':e.get('request'),'response':r})
    with (d/'turns.jsonl').open('w') as f:
        for t in turns: f.write(json.dumps(t,separators=(',',':'))+'\n')
    samples=[json.loads(x) for x in (d/'process-samples.jsonl').read_text().splitlines() if x.strip()]
    by={}
    for s in samples:
        t=s['observed_at']; by.setdefault(t,[]).append(s)
    ts=sorted(by); mem=[]
    for t in ts: mem.append(sum((x.get('pss_bytes') or 0) for x in by[t])/1024**2)
    cpu=[]
    for a,b in zip(ts,ts[1:]):
        rows=by[b]; cpu.append(sum((x.get('utime_ticks',0)+x.get('stime_ticks',0)) for x in rows))
    cpu=[0]+[min(100.0, x/100) for x in cpu]
    out=d/'resources.jsonl'
    with out.open('w') as f:
        for i,t in enumerate(ts): f.write(json.dumps({'observed_at':t,'sessions':1,'pss_mb':mem[i],'cpu_percent':cpu[i]})+'\n')
    fig,ax=plt.subplots(figsize=(8,4.8)); ax2=ax.twinx()
    ax.plot(range(1,len(mem)+1),mem,color='#2457a6',lw=1.8); ax2.plot(range(1,len(cpu)+1),cpu,color='#d05a32',lw=1.3)
    ax.set_xlabel('Sample'); ax.set_ylabel('PSS (MB)'); ax2.set_ylabel('CPU (%)'); ax.set_title('Mini-SWE-agent trace replay (1 session)')
    ax.grid(True,alpha=.25); fig.tight_layout(); fig.savefig(d/'sessions_cpu_memory.png',dpi=180); plt.close(fig)
    print(json.dumps({'run':str(d),'turns':len(turns),'samples':len(ts),'plot':str(d/'sessions_cpu_memory.png')}))
if __name__=='__main__': main(sys.argv[1])
