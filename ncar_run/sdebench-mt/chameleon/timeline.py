"""KV-cache use and host memory over time, per N of a run: N<n>/timeline.csv + N<n>/timeline.png,
plus <run>/timeline_summary.json (peaks). Usage: timeline.py <run_dir>

Series (from the 1 Hz samples.jsonl written by sdebench_mt/metrics.py):
  kv_gpu_pct      vLLM kv_cache_usage_perc x 100 (GPU KV cache in use)
  cpu_kv_fill_gb  offload arm: CPU KV cache filled = min(bytes stored since start, capacity). Stores are
                  deduplicated and chunks are freed only by LRU eviction once the cache is full, so this is
                  the fill level. (vLLM's kv_offload_cpu_cache_usage_perc counts only chunks in transfer.)
  Host memory = NUMA node 0 (Grace LPDDR5X) only. On GH200 the GPU's HBM is NUMA node 1 and Linux
  counts GPU allocations there as used memory, so system-wide MemTotal - MemAvailable is not host use.
  vllm_pinned_gb  vLLM shared/pinned host memory: RssShmem of the vLLM process tree. CUDA pinned host
                  allocations count as shared memory, so this holds the CPU KV buffer (offload arm,
                  68.7 GB), the UVA-offloaded weights (uva arm, 8.6 GB) and other pinned staging buffers.
                  Samples from before 2026-10-03 lack it; there it is the CPU KV buffer size.
  vllm_other_gb   other host memory of vLLM: RssAnon of its tree (heaps, Python)
  vm_rss_gb       microVMs: sum of cloud-hypervisor RSS
  other_used_gb   node-0 non-file memory (MemUsed - FilePages) minus vLLM anon and microVMs
  page_cache_gb   node-0 page cache (FilePages - Shmem, reclaimable; CSV only)
  hbm_node_used_gb NUMA node 1 MemUsed (GPU allocations)
  vllm_other/other_used/page_cache/hbm need the 2026-10-03 sampler; older runs show buffer + microVMs.
  running, waiting, n_vmm
"""
import csv, json, re, sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# Reference categorical slots 1-5 (dataviz skill palette, light surface), assigned in fixed order.
C = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3de"
GB = 1e9


def rows(p):
    return [json.loads(l) for l in open(p)] if Path(p).exists() else []


def log_value(d, pattern, cast=float):
    for log in (d / "vllm.log", d.parent / "vllm.log"):
        if log.exists():
            m = re.search(pattern, log.read_text(errors="replace"))
            if m:
                return cast(m.group(1).replace(",", ""))
    return None


def series(d):
    S = rows(d / "samples.jsonl")
    arm = json.load(open(d / "arm.json")).get("arm", "?") if (d / "arm.json").exists() else \
        (d.parent.name.split("_")[0].removeprefix("chi-") or "?")
    cap_gb = log_value(d, r"Created mmap file \S+ \(([\d.]+) GB\)")  # CPU KV buffer (offload arm)
    kv_tokens = log_value(d, r"GPU KV cache size: ([\d,]+) tokens", int)
    t0 = S[0]["t"] if S else 0
    st0 = next((s["vllm"].get("kv_offload_store_bytes_total") for s in S if "vllm" in s), 0) or 0
    out = []
    for s in S:
        v = s.get("vllm", {})
        vm = s.get("vllm_mem_kb")
        nodes = s.get("node", {})
        n0 = nodes.get("0")
        stored = (v.get("kv_offload_store_bytes_total", st0) or 0) - st0
        r = {
            "t_min": (s["t"] - t0) / 60,
            "kv_gpu_pct": 100 * v["kv_cache_usage_perc"] if "kv_cache_usage_perc" in v else np.nan,
            "cpu_kv_fill_gb": min(stored / GB, cap_gb) if cap_gb else np.nan,
            "vllm_pinned_gb": vm["shmem"] * 1024 / GB if vm else (cap_gb or 0.0),
            "vllm_other_gb": vm["anon"] * 1024 / GB if vm else np.nan,
            "vm_rss_gb": s.get("vmm_rss_kb", 0) * 1024 / GB,
            "page_cache_gb": (n0["FilePages"] - n0["Shmem"]) * 1024 / GB if n0 else np.nan,
            "hbm_node_used_gb": nodes.get("1", {}).get("MemUsed", np.nan) * 1024 / GB if "1" in nodes else np.nan,
            "running": v.get("num_requests_running", np.nan), "waiting": v.get("num_requests_waiting", np.nan),
            "n_vmm": s.get("n_vmm", 0),
        }
        r["other_used_gb"] = (max(0.0, (n0["MemUsed"] - n0["FilePages"]) * 1024 / GB - r["vllm_other_gb"] - r["vm_rss_gb"])
                              if n0 and vm else np.nan)
        out.append(r)
    return arm, cap_gb, kv_tokens, out


def style(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, lw=0.6)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(INK2)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.title.set_color(INK)


def plot(d, arm, cap_gb, kv_tokens, R):
    t = np.array([r["t_min"] for r in R])
    col = lambda k: np.array([r[k] for r in R], float)
    fig, ax = plt.subplots(4, 1, figsize=(11, 12), sharex=True, facecolor=SURFACE,
                           gridspec_kw={"height_ratios": [1, 1, 1.5, 1]})
    for a in ax:
        style(a)
    ttl = dict(loc="left", fontsize=11, fontweight="bold")
    ax[0].plot(t, col("kv_gpu_pct"), color=C[0], lw=1.5)
    ax[0].set_ylim(0, 105)
    ax[0].set_ylabel("% of capacity", color=INK2)
    ax[0].set_title(f"GPU KV cache in use (capacity {kv_tokens:,} tokens)" if kv_tokens else "GPU KV cache in use", **ttl)

    if cap_gb:
        ax[1].plot(t, col("cpu_kv_fill_gb"), color=C[0], lw=1.5)
        ax[1].axhline(cap_gb, color=INK2, lw=1, ls="--")
        ax[1].text(t[-1] if len(t) else 0, cap_gb, f"capacity {cap_gb:.1f} GB", color=INK2, fontsize=9, ha="right", va="bottom")
        ax[1].set_ylim(0, cap_gb * 1.12)
    else:
        ax[1].text(0.5, 0.5, "no CPU KV cache in this arm", transform=ax[1].transAxes, ha="center", va="center", color=INK2)
        ax[1].set_yticks([])
    ax[1].set_ylabel("GB", color=INK2)
    ax[1].set_title("CPU KV cache filled (offload arm)", **ttl)

    parts = [("vLLM pinned (CPU KV buffer, UVA weights)", "vllm_pinned_gb"), ("vLLM other (heaps)", "vllm_other_gb"),
             ("microVMs", "vm_rss_gb"), ("everything else in use", "other_used_gb")]
    parts = [(lbl, k) for lbl, k in parts if not np.all(np.isnan(col(k)))]
    ax[2].stackplot(t, *[np.nan_to_num(col(k)) for _, k in parts], labels=[p[0] for p in parts],
                    colors=C[:len(parts)], alpha=0.85, edgecolor=SURFACE, linewidth=0.6)
    top = np.nansum([np.nan_to_num(col(k)) for _, k in parts], axis=0).max() if parts else 1
    ax[2].set_ylim(0, max(top, 1) * 1.3)
    ax[2].set_ylabel("GB", color=INK2)
    partial = "vllm_other_gb" not in [k for _, k in parts]
    ax[2].set_title("Grace (host) memory in use, by consumer" +
                    (" (only vLLM pinned memory and microVMs recorded in this run)" if partial else ""), **ttl)
    ax[2].legend(loc="upper left", fontsize=9, frameon=False, ncol=len(parts), labelcolor=INK)

    for i, (lbl, k) in enumerate([("vLLM requests running", "running"), ("vLLM requests waiting", "waiting"), ("live microVMs", "n_vmm")]):
        ax[3].plot(t, col(k), color=C[i], lw=1.2, label=lbl)
    ax[3].set_ylim(0, max(1, np.nanmax([np.nanmax(col(k)) for k in ("running", "waiting", "n_vmm")])) * 1.3)
    ax[3].set_ylabel("count", color=INK2)
    ax[3].set_title("Load", **ttl)
    ax[3].legend(loc="upper left", fontsize=9, frameon=False, ncol=3, labelcolor=INK)
    ax[3].set_xlabel("minutes since start of run", color=INK2)
    fig.suptitle(f"{d.parent.name} · N={d.name[1:]} · arm {arm}", x=0.06, ha="left", color=INK, fontsize=13)
    fig.tight_layout()
    fig.savefig(d / "timeline.png", dpi=110, facecolor=SURFACE)
    plt.close(fig)


def main(run):
    run = Path(run)
    summ = []
    for d in sorted([p for p in run.glob("N*") if (p / "samples.jsonl").exists()], key=lambda p: int(p.name[1:])):
        arm, cap_gb, kv_tokens, R = series(d)
        if not R:
            continue
        with open(d / "timeline.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(R[0]))
            w.writeheader()
            w.writerows(R)
        plot(d, arm, cap_gb, kv_tokens, R)
        mx = lambda k: float(np.nanmax([r[k] for r in R])) if not all(np.isnan(r[k]) for r in R) else float("nan")
        summ.append({"N": int(d.name[1:]), "arm": arm, "minutes": R[-1]["t_min"], "kv_gpu_peak_pct": mx("kv_gpu_pct"),
                     "cpu_kv_fill_peak_gb": mx("cpu_kv_fill_gb"), "vllm_pinned_peak_gb": mx("vllm_pinned_gb"),
                     "vllm_other_peak_gb": mx("vllm_other_gb"), "vm_rss_peak_gb": mx("vm_rss_gb"),
                     "n_vmm_peak": mx("n_vmm"), "hbm_node_used_peak_gb": mx("hbm_node_used_gb")})
    (run / "timeline_summary.json").write_text(json.dumps(summ, indent=1))
    for s in summ:
        print("TIMELINE " + " ".join(f"{k}={v:.3g}" if isinstance(v, float) else f"{k}={v}" for k, v in s.items()))


if __name__ == "__main__":
    main(sys.argv[1])
