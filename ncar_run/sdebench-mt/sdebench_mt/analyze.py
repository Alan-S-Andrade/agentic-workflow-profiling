"""Summarize a sweep run directory (runs/<job>/N<n>/...) into a table + plots."""
import json, re, sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def rows(p):
    return [json.loads(l) for l in open(p)] if Path(p).exists() else []


def delta(samples, key):
    v = [s["vllm"][key] for s in samples if key in s.get("vllm", {})]
    return (v[-1] - v[0]) if len(v) > 1 else float("nan")


KV_BYTES_PER_TOKEN = 16 * 2 * 4 * 256  # Qwen3.8-27B: 16 full-attn layers x K,V x 4 kv heads x 256, fp8


def vllm_log_value(run, pattern):
    for log in [run / "vllm.log", *sorted(run.glob("N*/vllm.log"))]:
        m = re.search(pattern, log.read_text()) if log.exists() else None
        if m:
            return int(m.group(1).replace(",", ""))
    return None


def kv_capacity_tokens(run):
    return vllm_log_value(run, r"GPU KV cache size: ([\d,]+) tokens")


def kv_block_tokens(run):
    """Hybrid (linear-attention) models: vLLM enlarges the block (1568 tokens for Qwen3.8-27B)
    and, in mamba 'align' mode, caches state only at block boundaries."""
    return vllm_log_value(run, r"Setting attention block size to (\d+) tokens") or 16


def tenant_kv(ev, sample_t, block):
    """Per-conversation KV view built from each tenant's own call log.

    footprint      = prompt+completion tokens of the conversation's latest call (its history,
                     which vLLM may still hold as prefix cache) x bytes/token
    reusable       = previous call's prompt rounded down to a block boundary: the most the
                     next call can hit (block-aligned prefix caching)
    align recompute= previous prompt - reusable: re-prefilled every call even with no eviction
    evict recompute= reusable - cached: whole blocks evicted from GPU -> recomputed
    """
    runs = {}
    for e in ev:
        if e["kind"] == "llm" and e.get("ok"):
            runs.setdefault((e["tenant"], e["instance_id"]), []).append(e)
    lost_tok = align_tok = prompt_tok = cached_tok = 0
    n_evict = n_pairs = 0
    timelines = []
    for calls in runs.values():
        calls.sort(key=lambda e: e["started_at"])
        for a, b in zip(calls, calls[1:]):
            if b.get("cached_tokens") is None or a.get("prompt_tokens") is None:
                continue
            reusable = a["prompt_tokens"] // block * block
            lost = max(0, reusable - b["cached_tokens"])
            lost_tok += lost
            align_tok += a["prompt_tokens"] - reusable
            n_pairs += 1
            n_evict += lost >= block
        for e in calls:
            prompt_tok += e.get("prompt_tokens") or 0
            cached_tok += e.get("cached_tokens") or 0
        ends = np.array([e["started_at"] + e["duration_ms"] / 1000 for e in calls])
        ctx = np.array([(e.get("prompt_tokens") or 0) + (e.get("completion_tokens") or 0) for e in calls])
        idx = np.searchsorted(ends, sample_t, side="right") - 1
        alive = (idx >= 0) & (sample_t <= ends[-1])
        timelines.append(np.where(alive, ctx[np.clip(idx, 0, None)], 0))
    ws = np.sum(timelines, axis=0) if timelines else np.zeros_like(sample_t)
    per_conv = [c for t in timelines for c in t[t > 0]]
    return {
        "prefix_hit_tok": cached_tok / prompt_tok if prompt_tok and cached_tok else float("nan"),
        "evicted_frac": n_evict / n_pairs if n_pairs else float("nan"),
        "recomputed_Mtok": lost_tok / 1e6 if n_pairs else float("nan"),
        "align_recompute_Mtok": align_tok / 1e6 if n_pairs else float("nan"),
        "conv_kv_p50_gib": np.percentile(per_conv, 50) * KV_BYTES_PER_TOKEN / 2**30 if per_conv else float("nan"),
        "conv_kv_max_gib": max(per_conv) * KV_BYTES_PER_TOKEN / 2**30 if per_conv else float("nan"),
        "ws_max_ktok": ws.max() / 1e3 if len(ws) else float("nan"),
        "_ws": ws,
    }


def summarize(d, block=16):
    res, samples = rows(d / "results.jsonl"), rows(d / "samples.jsonl")
    ev = [e for f in (d / "tenants").glob("*/*.events.jsonl") for e in rows(f)]
    llm = [e for e in ev if e["kind"] == "llm" and e.get("ok")]
    tool = [e for e in ev if e["kind"] == "tool"]
    boot = [e["duration_ms"] / 1000 for e in ev if e["kind"] == "vm_boot"]
    cfg = json.loads((d / "config.json").read_text())
    span = (samples[-1]["t"] - samples[0]["t"]) if len(samples) > 1 else float("nan")
    lat = np.array([e["duration_ms"] / 1000 for e in llm]) if llm else np.array([np.nan])
    vl = [s["vllm"] for s in samples if "vllm" in s]
    kv_key = "kv_cache_usage_perc" if vl and "kv_cache_usage_perc" in vl[0] else "gpu_cache_usage_perc"
    ttft_n = delta(samples, "time_to_first_token_seconds_count")
    q_n = delta(samples, "request_queue_time_seconds_count")
    pc_q = delta(samples, "prefix_cache_queries_total")
    active_vmm = [s["vmm_rss_kb"] / s["n_vmm"] / 1024 for s in samples if s["n_vmm"]]
    return {
        "N": cfg["tenants"], "jobs": len(res), "resolved": sum(bool(r.get("resolved")) for r in res),
        "errors": sum(1 for r in res if r.get("error")), "makespan_min": span / 60,
        "jobs_per_hour": len(res) / span * 3600 if span else float("nan"),
        "llm_calls": len(llm), "llm_p50_s": np.nanpercentile(lat, 50), "llm_p95_s": np.nanpercentile(lat, 95),
        "prompt_tok_s": delta(samples, "prompt_tokens_total") / span,
        "gen_tok_s": delta(samples, "generation_tokens_total") / span,
        "ttft_mean_s": delta(samples, "time_to_first_token_seconds_sum") / ttft_n if ttft_n else float("nan"),
        "queue_mean_s": delta(samples, "request_queue_time_seconds_sum") / q_n if q_n else float("nan"),
        "prefix_hit": delta(samples, "prefix_cache_hits_total") / pc_q if pc_q else float("nan"),
        "preemptions": delta(samples, "num_preemptions_total"),
        "running_mean": np.mean([v.get("num_requests_running", 0) for v in vl]) if vl else float("nan"),
        "waiting_max": max([v.get("num_requests_waiting", 0) for v in vl], default=float("nan")),
        "kv_max_pct": 100 * max([v.get(kv_key, 0) for v in vl], default=float("nan")),
        "tool_p50_s": np.nanpercentile([e["duration_ms"] / 1000 for e in tool], 50) if tool else float("nan"),
        "tool_share": sum(e["duration_ms"] for e in tool) / max(1, sum(e["duration_ms"] for e in tool) + sum(e["duration_ms"] for e in llm)),
        "boot_mean_s": np.mean(boot) if boot else float("nan"),
        "vmm_rss_mean_mb": np.mean(active_vmm) if active_vmm else float("nan"),
        "host_avail_min_gb": min(s["mem_avail_kb"] for s in samples) / 2**20 if samples else float("nan"),
        "_samples": samples,
    } | tenant_kv(ev, np.array([x["t"] for x in samples]) if samples else np.array([]), block)


def main(run):
    run = Path(run)
    dirs = sorted([p for p in run.glob("N*") if (p / "config.json").exists()], key=lambda p: int(p.name[1:]))
    block = kv_block_tokens(run)
    S = [summarize(d, block) for d in dirs]
    cols = [k for k in S[0] if not k.startswith("_")] if S else []
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for s in S:
        lines.append("| " + " | ".join(f"{s[c]:.3g}" if isinstance(s[c], float) else str(s[c]) for c in cols) + " |")
    (run / "summary.md").write_text("\n".join(lines) + "\n")
    (run / "summary.json").write_text(json.dumps([{k: v for k, v in s.items() if not k.startswith("_")} for s in S],
                                                 indent=1, default=float))
    print("\n".join(lines))
    if not S:
        return
    N = [s["N"] for s in S]
    cap = kv_capacity_tokens(run)
    fig, ax = plt.subplots(3, 3, figsize=(15, 12))
    ax[0, 0].plot(N, [s["llm_p50_s"] for s in S], "o-", label="p50")
    ax[0, 0].plot(N, [s["llm_p95_s"] for s in S], "s--", label="p95")
    ax[0, 0].set(title="LLM call latency", xlabel="tenants", ylabel="s", xscale="log", xticks=N, xticklabels=N)
    ax[0, 0].legend()
    ax[0, 1].plot(N, [s["gen_tok_s"] for s in S], "o-", label="generation")
    ax[0, 1].plot(N, [s["prompt_tok_s"] for s in S], "s--", label="prompt")
    ax[0, 1].set(title="vLLM token throughput", xlabel="tenants", ylabel="tok/s", xscale="log", xticks=N, xticklabels=N)
    ax[0, 1].legend()
    ax[0, 2].plot(N, [s["jobs_per_hour"] for s in S], "o-")
    ax[0, 2].set(title="Instances completed / hour", xlabel="tenants", xscale="log", xticks=N, xticklabels=N)
    for s in S:
        sm = s["_samples"]
        if not sm:
            continue
        t = np.array([x["t"] for x in sm]) - sm[0]["t"]
        vl = [x.get("vllm", {}) for x in sm]
        kv = [100 * v.get("kv_cache_usage_perc", v.get("gpu_cache_usage_perc", 0)) for v in vl]
        ax[1, 0].plot(t / 60, kv, label=f"N={s['N']}")
        ax[1, 1].plot(t / 60, [v.get("num_requests_running", 0) + v.get("num_requests_waiting", 0) for v in vl], label=f"N={s['N']}")
        ax[1, 2].plot(t / 60, [x["vmm_rss_kb"] / 2**20 for x in sm], label=f"N={s['N']}")
        ax[2, 0].plot(t / 60, s["_ws"] / 1e3, label=f"N={s['N']}")
    if cap:
        ax[2, 0].axhline(cap / 1e3, color="k", ls=":", label="GPU KV capacity")
    ax[2, 0].set(title="Sum of live conversation contexts (KV demand)", xlabel="min", ylabel="k tokens")
    ax[2, 0].legend(fontsize=7)
    ax[2, 1].plot(N, [s["prefix_hit_tok"] for s in S], "o-", label="cached/prompt tokens")
    ax[2, 1].plot(N, [s["evicted_frac"] for s in S], "s--", label="calls with evicted prefix")
    ax[2, 1].set(title="Per-conversation prefix reuse", xlabel="tenants", xscale="log", xticks=N, xticklabels=N, ylim=(0, 1.05))
    ax[2, 1].legend(fontsize=7)
    ax[2, 2].plot(N, [s["recomputed_Mtok"] for s in S], "o-", label="evicted blocks")
    ax[2, 2].plot(N, [s["align_recompute_Mtok"] for s in S], "s--", label=f"block-alignment tail ({block} tok blocks)")
    ax[2, 2].set(title="Prefill recomputed per sweep step", xlabel="tenants", ylabel="M tokens", xscale="log", xticks=N, xticklabels=N)
    ax[2, 2].legend(fontsize=7)
    ax[1, 0].set(title="KV cache usage", xlabel="min", ylabel="%")
    ax[1, 1].set(title="vLLM running+waiting requests", xlabel="min")
    ax[1, 2].set(title="Total microVM RSS", xlabel="min", ylabel="GiB")
    for a in ax[1]:
        a.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(run / "sweep.png", dpi=120)
    print("wrote", run / "summary.md", run / "sweep.png")


if __name__ == "__main__":
    main(sys.argv[1])
