"""Side-by-side table of all arms of one sweep: runs/chi-<arm>_<SWEEP_ID>/{summary,recompute,timeline_summary}.json.
Usage: compare.py <SWEEP_ID> [--runs runs]   Writes runs/compare_<SWEEP_ID>.md and prints it.
"""
import argparse, json
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("sweep_id")
ap.add_argument("--runs", default="runs")
a = ap.parse_args()


def load(p):
    try:
        return {x["N"]: x for x in json.load(open(p))}
    except (OSError, ValueError):
        return {}


COLS = [  # (header, source, key, format)
    ("resolved", "s", None, None), ("errors", "s", "errors", "{:.0f}"), ("makespan min", "s", "makespan_min", "{:.0f}"),
    ("gen tok/s", "s", "gen_tok_s", "{:.0f}"), ("LLM p50 s", "s", "llm_p50_s", "{:.1f}"), ("LLM p95 s", "s", "llm_p95_s", "{:.0f}"),
    ("peak GPU KV %", "s", "kv_max_pct", "{:.0f}"), ("preempt", "s", "preemptions", "{:.0f}"),
    ("calls w/ evicted blocks", "r", "frac_calls_evict", "{:.1%}"), ("recompute / prefill tok", "r", "recompute_share_of_computed", "{:.0%}"),
    ("recompute GPU-s", "r", "recompute_gpu_s", "{:.0f}"), ("evict share GPU time", "r", "evict_share_of_gpu_time", "{:.2%}"),
    ("tail share GPU time", "r", None, None), ("GPU hits M tok", "r", "srv_gpu_hit_Mtok", "{:.1f}"),
    ("CPU hits M tok", "r", "srv_cpu_hit_Mtok", "{:.2f}"), ("CPU→GPU GB", "r", "offload_load_GB", "{:.1f}"),
    ("peak CPU KV fill GB", "t", "cpu_kv_fill_peak_gb", "{:.1f}"), ("peak VM RSS GB", "t", "vm_rss_peak_gb", "{:.1f}"),
]
lines = ["| arm | N | " + " | ".join(c[0] for c in COLS) + " |", "|" + "---|" * (len(COLS) + 2)]
for run in sorted(Path(a.runs).glob(f"chi-*_{a.sweep_id}")):
    arm = run.name.split("_")[0].removeprefix("chi-")
    S, R, T = load(run / "summary.json"), load(run / "recompute.json"), load(run / "timeline_summary.json")
    for n in sorted(S):
        src = {"s": S.get(n, {}), "r": R.get(n, {}), "t": T.get(n, {})}
        cells = []
        for hdr, so, key, fmt in COLS:
            d = src[so]
            if hdr == "resolved":
                cells.append(f"{d.get('resolved', '?')}/{d.get('jobs', '?')}")
            elif hdr == "tail share GPU time":
                ms = d.get("makespan_s")
                cells.append(f"{d['tail_gpu_s'] / ms:.2%}" if d.get("tail_gpu_s") is not None and ms else "–")
            else:
                v = d.get(key)
                cells.append(fmt.format(v) if isinstance(v, (int, float)) and v == v else "–")
        lines.append(f"| {arm} | {n} | " + " | ".join(cells) + " |")
out = "\n".join(lines) + "\n"
Path(a.runs, f"compare_{a.sweep_id}.md").write_text(out)
print(out)
