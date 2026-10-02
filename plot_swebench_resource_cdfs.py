#!/usr/bin/env python3
"""Plot SWE-bench replay CPU and memory distributions by concurrency.

Each completed ``sessions-NNN/turn-resource.jsonl`` file contributes one
observation per measured agent turn.  The script writes:

* ``<prefix>-cdf.png``: CPU-utilization and memory empirical CDFs.  Curves
  are grouped and colored by the number of simultaneous sessions.
* ``<prefix>-scale.png``: the requested session-count x-axis, with CPU CDF
  percentiles on the left y-axis and memory CDF percentiles on the right.

A CDF needs the resource value on its horizontal axis.  The companion scale
plot therefore uses the session count on the horizontal axis and shows CDF
percentiles (P50/P95/P99) on the two vertical axes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


GIB = 1024.0**3


def load_stage_rows(results: Path, memory_field: str) -> dict[int, dict[str, np.ndarray]]:
    grouped: dict[int, dict[str, list[float]]] = {}
    for stage_dir in sorted(results.glob("sessions-*")):
        if not stage_dir.is_dir():
            continue
        try:
            concurrency = int(stage_dir.name.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        resource_file = stage_dir / "turn-resource.jsonl"
        summary_file = stage_dir / "summary.json"
        if not resource_file.exists() or not summary_file.exists():
            continue
        summary = json.loads(summary_file.read_text())
        if summary.get("memory_guard_triggered"):
            continue
        cpu_values: list[float] = []
        memory_values: list[float] = []
        for line in resource_file.read_text().splitlines():
            row = json.loads(line)
            started, finished = row.get("started_at"), row.get("finished_at")
            cpu_seconds = row.get("cpu_seconds")
            memory_bytes = row.get(memory_field)
            if not isinstance(started, (int, float)) or not isinstance(finished, (int, float)):
                continue
            if not isinstance(cpu_seconds, (int, float)) or not isinstance(memory_bytes, (int, float)):
                continue
            duration = float(finished) - float(started)
            if duration <= 0 or cpu_seconds < 0 or memory_bytes < 0:
                continue
            # One hundred percent means one fully busy CPU for the turn.
            cpu_values.append(100.0 * float(cpu_seconds) / duration)
            memory_values.append(float(memory_bytes) / GIB)
        if cpu_values and memory_values:
            grouped[concurrency] = {
                "cpu_util_pct": np.asarray(cpu_values, dtype=float),
                "memory_gib": np.asarray(memory_values, dtype=float),
            }
    if not grouped:
        raise SystemExit(f"no completed turn-resource data found under {results}")
    return grouped


def empirical_cdf(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = np.sort(values)
    y = np.arange(1, len(x) + 1, dtype=float) / len(x)
    return x, y


def percentile_table(grouped: dict[int, dict[str, np.ndarray]]) -> dict[int, dict[str, dict[int, float]]]:
    return {
        count: {
            metric: {p: float(np.percentile(values, p)) for p in (50, 95, 99)}
            for metric, values in metrics.items()
        }
        for count, metrics in grouped.items()
    }


def plot_cdfs(grouped: dict[int, dict[str, np.ndarray]], output: Path) -> None:
    counts = sorted(grouped)
    colors = plt.get_cmap("viridis")(np.linspace(0.08, 0.92, len(counts)))
    fig, (cpu_ax, mem_ax) = plt.subplots(1, 2, figsize=(14, 5.5), constrained_layout=True)
    for color, count in zip(colors, counts):
        cpu_x, cpu_y = empirical_cdf(grouped[count]["cpu_util_pct"])
        mem_x, mem_y = empirical_cdf(grouped[count]["memory_gib"])
        cpu_ax.step(cpu_x, cpu_y, where="post", color=color, linewidth=1.8, label=f"{count} sessions")
        mem_ax.step(mem_x, mem_y, where="post", color=color, linewidth=1.8, label=f"{count} sessions")
    cpu_ax.set(title="Per-turn CPU utilization CDF", xlabel="CPU utilization (% of one CPU)", ylabel="CDF")
    mem_ax.set(title="Per-turn memory usage CDF", xlabel="Peak cgroup memory (GiB)", ylabel="CDF")
    for ax in (cpu_ax, mem_ax):
        ax.set_ylim(0, 1.02)
        ax.grid(True, alpha=0.25)
    cpu_ax.legend(title="Concurrency", loc="lower right", fontsize=8)
    fig.suptitle("SWE-bench replay resource distributions")
    fig.savefig(output, dpi=180)
    plt.close(fig)


def plot_scale(grouped: dict[int, dict[str, np.ndarray]], output: Path) -> None:
    counts = np.asarray(sorted(grouped), dtype=int)
    table = percentile_table(grouped)
    fig, cpu_ax = plt.subplots(figsize=(10, 6), constrained_layout=True)
    mem_ax = cpu_ax.twinx()
    styles = {50: "-", 95: "--", 99: ":"}
    cpu_lines, mem_lines = [], []
    for p, style in styles.items():
        cpu_values = [table[int(c)]["cpu_util_pct"][p] for c in counts]
        mem_values = [table[int(c)]["memory_gib"][p] for c in counts]
        cpu_line, = cpu_ax.plot(counts, cpu_values, color="tab:blue", linestyle=style, marker="o", label=f"CPU P{p}")
        mem_line, = mem_ax.plot(counts, mem_values, color="tab:red", linestyle=style, marker="s", label=f"Memory P{p}")
        cpu_lines.append(cpu_line)
        mem_lines.append(mem_line)
    cpu_ax.set_xlabel("Simultaneous sessions")
    cpu_ax.set_ylabel("CPU utilization (% of one CPU)", color="tab:blue")
    mem_ax.set_ylabel("Peak memory (GiB)", color="tab:red")
    cpu_ax.set_xticks(counts)
    cpu_ax.grid(True, alpha=0.25)
    cpu_ax.legend(handles=cpu_lines + mem_lines, loc="best", fontsize=9)
    fig.suptitle("Resource CDF percentiles versus concurrency")
    fig.savefig(output, dpi=180)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, help="experiment output directory")
    parser.add_argument("--output-prefix", type=Path, help="prefix for the two PNG files")
    parser.add_argument("--memory-field", choices=("peak_cgroup_memory_bytes", "peak_pss_bytes"),
                        default="peak_cgroup_memory_bytes",
                        help="memory column from turn-resource.jsonl (default: cgroup memory)")
    args = parser.parse_args()
    prefix = args.output_prefix or (args.results / "resource")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    grouped = load_stage_rows(args.results, args.memory_field)
    plot_cdfs(grouped, prefix.with_name(prefix.name + "-cdf.png"))
    plot_scale(grouped, prefix.with_name(prefix.name + "-scale.png"))
    print(f"wrote {prefix}-cdf.png")
    print(f"wrote {prefix}-scale.png")


if __name__ == "__main__":
    main()
