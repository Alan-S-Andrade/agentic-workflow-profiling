"""How many LLM calls recompute KV, and how much GPU time that costs.

Usage: recompute.py <run_dir> [--cost prefill_cost.json]
Reads runs/<run>/N<n>/{tenants/*/*.events.jsonl, samples.jsonl, prefill_cost.json, arm.json} and the
block size from vllm.log; writes <run_dir>/recompute.md, recompute.json and N<n>/calls_recompute.csv.

Per call b following call a of the same conversation (tenant, instance), with block size B
(1568 tokens; prefix caching is block-granular, and in mamba 'align' mode so is the recurrent state):
  cached   = b's usage.prompt_tokens_details.cached_tokens (GPU hits + CPU/offload hits)
  computed = b.prompt - cached                        tokens actually prefilled for b
  reusable = floor(a.prompt / B) * B                  the most b could have hit
  evict    = max(0, reusable - cached)                whole blocks cached before, now gone -> recomputed
  tail     = max(0, a.prompt - max(reusable, cached)) partial last block of a, recomputed every call
  recompute= evict + tail                             = tokens a already prefilled that b prefills again
  new      = computed - recompute                     a's visible output + the new observation
First calls of a conversation have no predecessor: all their computed tokens count as "first".
GPU time of prefilling n tokens starting at context c: a*n + b*n*(c + n/2) (prefill_cost.py fit).
"""
import argparse, csv, json, re
from pathlib import Path
import numpy as np


def rows(p):
    return [json.loads(l) for l in open(p)] if Path(p).exists() else []


def gpu_s(cost, n, c):
    return cost["a_s_per_tok"] * n + cost["b_s_per_tok2"] * n * (c + n / 2) if n > 0 else 0.0


def block_tokens(d):
    for log in (d / "vllm.log", d.parent / "vllm.log"):
        if log.exists():
            m = re.search(r"Setting attention block size to (\d+) tokens", log.read_text(errors="replace"))
            if m:
                return int(m.group(1))
    return 1568


def counter_delta(samples, key):
    v = [s["vllm"][key] for s in samples if key in s.get("vllm", {})]
    return (v[-1] - v[0]) if len(v) > 1 else 0.0


def analyze(d, cost_override=None):
    cost_p = cost_override or next((p for p in (d / "prefill_cost.json", d.parent / "prefill_cost.json") if p.exists()), None)
    cost = json.load(open(cost_p)) if cost_p else None
    B = block_tokens(d)
    arm = json.load(open(d / "arm.json")) if (d / "arm.json").exists() else {}
    ev = [e for f in (d / "tenants").glob("*/*.events.jsonl") for e in rows(f)]
    convs = {}
    for e in ev:
        if e.get("kind") == "llm" and e.get("ok") and e.get("prompt_tokens") is not None:
            convs.setdefault((e["tenant"], e["instance_id"]), []).append(e)
    calls = []
    for (ten, iid), cs in convs.items():
        cs.sort(key=lambda e: e["started_at"])
        prev = None
        for e in cs:
            p, cached = e["prompt_tokens"], e.get("cached_tokens") or 0
            r = {"tenant": ten, "instance_id": iid, "started_at": e["started_at"], "duration_s": e["duration_ms"] / 1000,
                 "prompt": p, "cached": cached, "computed": p - cached, "first": 0, "evict": 0, "tail": 0, "new": 0}
            if prev is None:
                r["first"] = p - cached
            else:
                reusable = prev // B * B
                r["evict"] = max(0, reusable - cached)
                r["tail"] = max(0, prev - max(reusable, cached))
                r["new"] = max(0, r["computed"] - r["evict"] - r["tail"])
            r["recompute"] = r["evict"] + r["tail"]
            if cost:
                r["gpu_s_total"] = gpu_s(cost, r["computed"], cached)
                r["gpu_s_evict"] = gpu_s(cost, r["evict"], cached)
                r["gpu_s_tail"] = gpu_s(cost, r["tail"], max(cached, (prev or 0) // B * B))
                r["gpu_s_recompute"] = r["gpu_s_evict"] + r["gpu_s_tail"]
                r["gpu_s_cached"] = gpu_s(cost, cached, 0)
            calls.append(r)
            prev = p
    samples = rows(d / "samples.jsonl")
    span = samples[-1]["t"] - samples[0]["t"] if len(samples) > 1 else float("nan")
    succ = [c for c in calls if not c["first"] and c["prompt"] > 0]  # calls with a predecessor
    tot = lambda k, L=calls: float(sum(c.get(k, 0) for c in L))
    pct = lambda k, q: float(np.percentile([c[k] for c in succ], q)) if succ else float("nan")
    s = {
        "N": int(d.name[1:]), "arm": arm.get("arm", "?"), "block": B, "calls": len(calls), "followup_calls": len(succ),
        "calls_evict_ge1block": sum(c["evict"] >= B for c in succ),
        "calls_any_recompute": sum(c["recompute"] > 0 for c in succ),
        "frac_calls_evict": sum(c["evict"] >= B for c in succ) / len(succ) if succ else float("nan"),
        "recompute_tok_p50": pct("recompute", 50), "recompute_tok_p95": pct("recompute", 95),
        "evict_tok_p95": pct("evict", 95), "evict_tok_max": max([c["evict"] for c in succ], default=0),
        "prompt_Mtok": tot("prompt") / 1e6, "cached_Mtok": tot("cached") / 1e6, "computed_Mtok": tot("computed") / 1e6,
        "evict_Mtok": tot("evict") / 1e6, "tail_Mtok": tot("tail") / 1e6, "new_Mtok": tot("new") / 1e6, "first_Mtok": tot("first") / 1e6,
        "recompute_share_of_computed": (tot("evict") + tot("tail")) / tot("computed") if tot("computed") else float("nan"),
        "makespan_s": span,
        # server-side counters (GPU hits vs offload/CPU hits, transfers)
        "srv_prompt_Mtok": counter_delta(samples, "prompt_tokens_total") / 1e6,
        "srv_gpu_hit_Mtok": counter_delta(samples, "prefix_cache_hits_total") / 1e6,
        "srv_cpu_hit_Mtok": counter_delta(samples, "external_prefix_cache_hits_total") / 1e6,
        "offload_load_GB": counter_delta(samples, "kv_offload_load_bytes_total") / 1e9,
        "offload_store_GB": counter_delta(samples, "kv_offload_store_bytes_total") / 1e9,
        "offload_load_s": counter_delta(samples, "kv_offload_load_time_total"),
        "offload_store_s": counter_delta(samples, "kv_offload_store_time_total"),
        "preemptions": counter_delta(samples, "num_preemptions_total"),
        "offload_alloc_failures": counter_delta(samples, "kv_offload_allocation_failure_total"),
        "llm_call_s": tot("duration_s"),
    }
    s["offload_load_GBps"] = s["offload_load_GB"] / s["offload_load_s"] if s["offload_load_s"] else float("nan")
    s["offload_store_GBps"] = s["offload_store_GB"] / s["offload_store_s"] if s["offload_store_s"] else float("nan")
    if cost:
        g_all, g_rec, g_ev = tot("gpu_s_total"), tot("gpu_s_recompute"), tot("gpu_s_evict")
        hits = s["srv_gpu_hit_Mtok"] + s["srv_cpu_hit_Mtok"]
        lat_share = [c["gpu_s_recompute"] / c["duration_s"] for c in succ if c["duration_s"] > 0]
        s |= {
            "prefill_gpu_s": g_all, "recompute_gpu_s": g_rec, "evict_gpu_s": g_ev, "tail_gpu_s": tot("gpu_s_tail"),
            "recompute_share_of_prefill_time": g_rec / g_all if g_all else float("nan"),
            "recompute_share_of_gpu_time": g_rec / span if span == span and span > 0 else float("nan"),
            "evict_share_of_gpu_time": g_ev / span if span == span and span > 0 else float("nan"),
            "recompute_share_of_llm_call_time": g_rec / s["llm_call_s"] if s["llm_call_s"] else float("nan"),
            "recompute_share_of_call_p50": float(np.percentile(lat_share, 50)) if lat_share else float("nan"),
            "recompute_share_of_call_p95": float(np.percentile(lat_share, 95)) if lat_share else float("nan"),
            # GPU time the CPU hits avoided: cost of prefilling each call's cached prefix from scratch,
            # scaled by the CPU share of all prefix hits (the per-call GPU/CPU split is not exposed)
            "cpu_hits_saved_gpu_s_est": tot("gpu_s_cached") * s["srv_cpu_hit_Mtok"] / hits if hits else 0.0,
            "cost_file": str(cost_p),
        }
    if calls:
        with open(d / "calls_recompute.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(calls[0]))
            w.writeheader()
            w.writerows(sorted(calls, key=lambda c: c["started_at"]))
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--cost", type=Path, help="prefill_cost.json to use when a N dir has none")
    a = ap.parse_args()
    run = Path(a.run)
    dirs = sorted([p for p in run.glob("N*") if (p / "tenants").exists()], key=lambda p: int(p.name[1:]))
    S = [analyze(d, None if (d / "prefill_cost.json").exists() else a.cost) for d in dirs]
    (run / "recompute.json").write_text(json.dumps(S, indent=1, default=float))
    keys = ["N", "arm", "followup_calls", "calls_evict_ge1block", "frac_calls_evict", "calls_any_recompute",
            "recompute_tok_p50", "recompute_tok_p95", "computed_Mtok", "evict_Mtok", "tail_Mtok", "new_Mtok",
            "recompute_share_of_computed", "srv_gpu_hit_Mtok", "srv_cpu_hit_Mtok", "offload_load_GB", "offload_load_GBps",
            "recompute_gpu_s", "recompute_share_of_prefill_time", "recompute_share_of_gpu_time",
            "evict_share_of_gpu_time", "recompute_share_of_call_p50", "recompute_share_of_call_p95",
            "cpu_hits_saved_gpu_s_est", "preemptions"]
    lines = ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
    for s in S:
        lines.append("| " + " | ".join(f"{s.get(k, float('nan')):.3g}" if isinstance(s.get(k), float) else str(s.get(k, "")) for k in keys) + " |")
    (run / "recompute.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
