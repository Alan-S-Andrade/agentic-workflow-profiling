#!/usr/bin/env python3
"""Plot aggregate host memory and CPU usage across experiment stages."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from collections import defaultdict
from pathlib import Path

try:
    import matplotlib.pyplot as plt
except ModuleNotFoundError:
    plt = None

STAGE_RE = re.compile(r"sessions-(\d+)$")


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open() as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def stage_points(stage_dir: Path, bin_seconds: float,
                 cpu_capacity_cores: int, memory_capacity_gib: float,
                 concurrency: int):
    host_sample_file = stage_dir / "host-samples.jsonl"
    if host_sample_file.exists():
        rows = read_jsonl(host_sample_file)
        if not rows:
            raise ValueError(f"no host samples in {stage_dir}")
        stage_start = min(float(row["timestamp"]) for row in rows)
        previous = None
        latest: dict[float, dict] = {}
        for row in rows:
            timestamp = float(row["timestamp"])
            bucket = math.floor((timestamp - stage_start) / bin_seconds) * bin_seconds
            latest[bucket] = row
        points = []
        for timestamp in sorted(latest):
            row = latest[timestamp]
            if row.get("numa_node_memory_total_bytes"):
                memory_percent = (100.0 * float(row["numa_node_memory_used_bytes"])
                                  / float(row["numa_node_memory_total_bytes"]))
            else:
                memory_percent = (100.0 * float(row["host_memory_used_bytes"])
                                  / float(row["host_memory_total_bytes"]))
            cpu_percent = 0.0
            if previous is not None:
                total_delta = (int(row["host_cpu_total_ticks"])
                                - int(previous["host_cpu_total_ticks"]))
                idle_delta = (int(row["host_cpu_idle_ticks"])
                              - int(previous["host_cpu_idle_ticks"]))
                if total_delta > 0 and idle_delta >= 0:
                    cpu_percent = 100.0 * (total_delta - idle_delta) / total_delta
            points.append((timestamp, memory_percent, cpu_percent,
                           int(row.get("resident_sessions", concurrency))))
            previous = row
        return stage_start, points

    rows = read_jsonl(stage_dir / "session-samples.jsonl")
    if not rows:
        raise ValueError(f"no samples in {stage_dir}")
    stage_start = min(float(row["timestamp"]) for row in rows)
    by_session: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_session[str(row["session"])].append(row)

    bins: dict[float, dict[str, tuple[float, float]]] = defaultdict(dict)
    for session, session_rows in by_session.items():
        session_rows.sort(key=lambda row: float(row["timestamp"]))
        previous = None
        for row in session_rows:
            timestamp = float(row["timestamp"])
            cpu_cores = 0.0
            if previous is not None:
                elapsed = timestamp - float(previous["timestamp"])
                cpu_delta = float(row["cpu_usage_usec"]) - float(previous["cpu_usage_usec"])
                if elapsed > 0 and cpu_delta >= 0:
                    cpu_cores = cpu_delta / 1e6 / elapsed
            bucket = math.floor((timestamp - stage_start) / bin_seconds) * bin_seconds
            bins[bucket][session] = (
                float(row["cgroup_memory_bytes"]) / 1024**3,
                cpu_cores,
            )
            previous = row

    points = []
    for timestamp in sorted(bins):
        values = bins[timestamp].values()
        points.append((timestamp,
                       100.0 * sum(value[0] for value in values) / memory_capacity_gib,
                       100.0 * sum(value[1] for value in values) / cpu_capacity_cores,
                       len(values)))
    return stage_start, points


def discover_stages(trace_root: Path) -> dict[int, Path]:
    stages = {}
    for path in trace_root.glob("sessions-*"):
        match = STAGE_RE.fullmatch(path.name)
        if match:
            stages[int(match.group(1))] = path
    return stages


def comparison_series(trace_root: Path, label: str, bin_seconds: float,
                      concurrency: int = 32) -> dict:
    """Extract one concurrency timeline, stopping at its memory guard."""
    stage_dir = discover_stages(trace_root).get(concurrency)
    if stage_dir is None:
        raise SystemExit(f"no sessions-{concurrency:03d} stage found under {trace_root}")
    host = read_jsonl(stage_dir / "host-samples.jsonl")
    cgroup = read_jsonl(stage_dir / "session-samples.jsonl")
    if not host or not cgroup:
        raise SystemExit(f"missing host or cgroup samples in {stage_dir}")
    guards = [row for row in host if row.get("event") == "memory_guard"]
    stop = float(guards[-1]["timestamp"]) if guards else max(float(r["timestamp"]) for r in host)
    host = [row for row in host if float(row["timestamp"]) <= stop]
    start = min(float(row["timestamp"]) for row in host)

    cpu, sessions = [], []
    previous = None
    for row in host:
        timestamp = float(row["timestamp"])
        elapsed = (timestamp - start) / 60.0
        sessions.append((elapsed, int(row.get("resident_sessions", 0))))
        if previous is not None:
            ticks = int(row["host_cpu_total_ticks"]) - int(previous["host_cpu_total_ticks"])
            idle = int(row["host_cpu_idle_ticks"]) - int(previous["host_cpu_idle_ticks"])
            if ticks > 0 and idle >= 0:
                cpu.append((elapsed, 100.0 * (ticks - idle) / ticks))
        previous = row

    cgroup = sorted((row for row in cgroup if float(row["timestamp"]) <= stop),
                    key=lambda row: float(row["timestamp"]))
    latest_by_session: dict[int, int] = {}
    sample_index = 0
    memory = []
    duration = stop - start
    for bucket in range(math.ceil(duration / bin_seconds) + 1):
        elapsed_seconds = min(bucket * bin_seconds, duration)
        timestamp = start + elapsed_seconds
        while sample_index < len(cgroup) and float(cgroup[sample_index]["timestamp"]) <= timestamp:
            row = cgroup[sample_index]
            latest_by_session[int(row["session"])] = int(
                row.get("cgroup_memory_bytes") or row.get("vmm_pss_bytes") or 0)
            sample_index += 1
        memory.append((elapsed_seconds / 60.0,
                       sum(latest_by_session.values()) / 1024**3))

    summary = json.loads((stage_dir / "summary.json").read_text())
    return {"label": label, "memory": memory, "cpu": cpu, "sessions": sessions,
            "duration_minutes": duration / 60.0,
            "peak_sessions": int(summary["peak_resident_sessions"]),
            "peak_memory_gib": int(summary.get("peak_aggregate_cgroup_memory_bytes") or
                                   summary.get("peak_aggregate_vmm_pss_bytes") or 0) / 1024**3,
            "guarded": bool(summary["memory_guard_triggered"])}


def plot_comparison(first_root: Path, second_root: Path, output: Path,
                    bin_seconds: float, first_label: str = "Node 0 only",
                    second_label: str = "TPP enabled",
                    concurrency: int = 32) -> None:
    """Render memory, CPU, and resident-session timelines for both runs."""
    runs = [comparison_series(first_root, first_label, bin_seconds, concurrency),
            comparison_series(second_root, second_label, bin_seconds, concurrency)]
    colors = {"memory": "tab:blue", "cpu": "tab:orange", "sessions": "tab:green"}
    styles = ["-", "--"]
    fig, memory_axis = plt.subplots(figsize=(12, 6.5), constrained_layout=True)
    cpu_axis = memory_axis.twinx()
    sessions_axis = memory_axis.twinx()
    sessions_axis.spines["right"].set_position(("outward", 58))
    for index, run in enumerate(runs):
        style = styles[index]
        memory_axis.plot([x for x, _ in run["memory"]], [y for _, y in run["memory"]],
                         color=colors["memory"], linestyle=style, linewidth=1.9,
                         label=f'VM cgroup memory — {run["label"]}')
        cpu_axis.plot([x for x, _ in run["cpu"]], [y for _, y in run["cpu"]],
                      color=colors["cpu"], linestyle=style, linewidth=1.5,
                      label=f'Host CPU — {run["label"]}')
        sessions_axis.step([x for x, _ in run["sessions"]], [y for _, y in run["sessions"]],
                           where="post", color=colors["sessions"], linestyle=style,
                           linewidth=1.9, label=f'Resident sessions — {run["label"]}')
        if run["guarded"]:
            end = run["duration_minutes"]
            memory_axis.axvline(end, color="0.35", linestyle=style, linewidth=1, alpha=0.8)
            sessions_axis.annotate(f'{run["label"]} guard ({run["peak_sessions"]})',
                                   xy=(end, run["peak_sessions"]),
                                   xytext=(5, -16 if index == 0 else 8),
                                   textcoords="offset points", fontsize=8,
                                   color=colors["sessions"])

    memory_axis.set_title(f"SWE-bench microVM resource usage: {first_label} vs {second_label}")
    memory_axis.set_xlabel("Minutes since experiment start")
    memory_axis.set_ylabel("Aggregate VM cgroup memory (GiB)", color=colors["memory"])
    cpu_axis.set_ylabel("Host CPU utilization on sampled CPUs (%)", color=colors["cpu"])
    sessions_axis.set_ylabel("Resident sessions", color=colors["sessions"])
    memory_axis.tick_params(axis="y", labelcolor=colors["memory"])
    cpu_axis.tick_params(axis="y", labelcolor=colors["cpu"])
    sessions_axis.tick_params(axis="y", labelcolor=colors["sessions"])
    memory_axis.set_ylim(bottom=0)
    cpu_axis.set_ylim(0, 100)
    sessions_axis.set_ylim(0, max(32, max(run["peak_sessions"] for run in runs) + 2))
    memory_axis.set_xlim(left=0, right=max(run["duration_minutes"] for run in runs) * 1.03)
    memory_axis.grid(True, alpha=0.25)
    handles, labels = [], []
    for axis in (memory_axis, cpu_axis, sessions_axis):
        h, l = axis.get_legend_handles_labels()
        handles.extend(h)
        labels.extend(l)
    memory_axis.legend(handles, labels, loc="upper left", fontsize=8, ncol=2)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180)
    plt.close(fig)
    print(f"wrote {output}")
    for run in runs:
        print(f'{run["label"]}: peak sessions={run["peak_sessions"]}, '
              f'peak cgroup memory={run["peak_memory_gib"]:.2f} GiB, '
              f'duration={run["duration_minutes"]:.2f} min')


def plot_usage_cdf(plotted_points: list[tuple], output: Path) -> None:
    """Plot empirical CDFs for aggregate memory and CPU utilization."""
    memory = sorted(point[1] for point in plotted_points)
    cpu = sorted(point[2] for point in plotted_points)
    if not memory or not cpu:
        raise ValueError("cannot plot CDF without resource samples")
    memory_cdf = [100.0 * index / len(memory) for index in range(1, len(memory) + 1)]
    cpu_cdf = [100.0 * index / len(cpu) for index in range(1, len(cpu) + 1)]

    fig, axis = plt.subplots(figsize=(10, 6), constrained_layout=True)
    axis.plot(memory, memory_cdf, color="tab:blue", linewidth=1.5,
              label="Memory utilization")
    axis.plot(cpu, cpu_cdf, color="tab:orange", linewidth=1.5,
              label="System-wide CPU utilization")
    axis.set_xlabel("Utilization (%)")
    axis.set_ylabel("Cumulative probability (%)")
    axis.set_title("SWE-bench microVM resource utilization CDF")
    axis.set_xlim(left=0)
    axis.set_ylim(0, 100)
    axis.grid(True, alpha=0.25)
    axis.legend(loc="lower right")
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=160)
    plt.close(fig)
    print(f"wrote {output}")


def host_memory_gib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return float(line.split()[1]) / 1024**2
    raise RuntimeError("could not read MemTotal from /proc/meminfo")


def plot(trace_root: Path, output: Path, requested_stages: list[int],
         bin_seconds: float, cpu_capacity_cores: int,
         memory_capacity_gib: float, cdf_output: Path) -> None:
    measured = {}
    for concurrency, stage_dir in sorted(discover_stages(trace_root).items()):
        sample_file = stage_dir / "session-samples.jsonl"
        if sample_file.exists():
            try:
                measured[concurrency] = stage_points(
                    stage_dir, bin_seconds, cpu_capacity_cores, memory_capacity_gib,
                    concurrency)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"warning: skipping {stage_dir}: {exc}")
    if not measured:
        raise SystemExit(f"no session-samples.jsonl files found under {trace_root}")

    global_start = min(start for start, _ in measured.values())
    stage_starts = {stage: (start - global_start) / 60.0
                    for stage, (start, _) in measured.items()}
    plotted_points = []
    for stage, (stage_start, points) in measured.items():
        offset = stage_starts[stage]
        minutes = [offset + (timestamp - stage_start) / 60.0
                   for timestamp, _, _, _ in points]
        memory = [memory for _, memory, _, _ in points]
        cpu = [cpu for _, _, cpu, _ in points]
        sessions = [sessions for _, _, _, sessions in points]
        plotted_points.extend(zip(minutes, memory, cpu, sessions))
    plotted_points.sort(key=lambda point: point[0])

    if plt is None:
        stage_plan = next((stage / "run-plan.json" for stage in discover_stages(trace_root).values()
                           if (stage / "run-plan.json").exists()), None)
        plan = json.loads(stage_plan.read_text()) if stage_plan else {}
        tiered = "external tier controller" in plan.get("resource_policy", "")
        subtitle = ("Node 0 is the fast memory tier; resident sessions reflect staggered admission."
                    if tiered else "Memory use is measured on the selected NUMA node.")
        guard_percent = None if tiered else plan.get("host_memory_threshold_percent")
        write_svg_plot(output, plotted_points, stage_starts, requested_stages,
                       subtitle, guard_percent)
        print(f"wrote {output}")
        print("measured stages:", ", ".join(str(stage) for stage in sorted(measured)))
        print("CDF skipped: matplotlib is not installed; the three-axis SVG was rendered with the standard library")
        return

    fig, memory_axis = plt.subplots(figsize=(12, 6), constrained_layout=True)
    cpu_axis = memory_axis.twinx()
    sessions_axis = memory_axis.twinx()
    sessions_axis.spines.right.set_position(("outward", 62))
    minutes = [point[0] for point in plotted_points]
    memory_percent = [point[1] for point in plotted_points]
    cpu_values = [point[2] for point in plotted_points]
    memory_axis.plot(minutes, memory_percent, color="tab:blue", linewidth=1.5,
                     label="Memory")
    cpu_axis.plot(minutes, cpu_values, color="tab:orange", linewidth=1.5,
                  label="System-wide CPU utilization")
    session_values = [point[3] for point in plotted_points]
    sessions_axis.step(minutes, session_values, where="post", color="tab:green",
                       linewidth=1.5, label="Concurrent sessions")
    cpu_label = "Host CPU utilization on sampled CPUs (%)"
    memory_axis.set_ylabel("Memory utilization (%)", color="tab:blue")
    memory_axis.tick_params(axis="y", labelcolor="tab:blue")
    cpu_axis.set_ylabel(cpu_label, color="tab:orange")
    cpu_axis.tick_params(axis="y", labelcolor="tab:orange")
    sessions_axis.set_ylabel("Concurrent sessions", color="tab:green")
    sessions_axis.tick_params(axis="y", labelcolor="tab:green")
    memory_axis.set_xlabel("Minutes since experiment start")
    memory_axis.set_title("SWE-bench microVM resource usage")
    memory_axis.grid(True, alpha=0.25)
    cpu_max = max(100.0, max(cpu_values, default=0.0) * 1.05)
    cpu_axis.set_ylim(0, cpu_max)
    memory_axis.legend(loc="upper left")
    cpu_axis.legend(loc="upper right")
    sessions_axis.set_ylim(0, max(requested_stages, default=1) * 1.15)
    sessions_axis.legend(loc="center right")

    memory_max = max(100.0, max(point[1] for point in plotted_points) * 1.05)
    memory_axis.set_ylim(0, memory_max)
    for concurrency in requested_stages:
        x = stage_starts.get(concurrency)
        if x is None:
            stage_dir = discover_stages(trace_root).get(concurrency)
            plan = stage_dir / "run-plan.json" if stage_dir else None
            if plan and plan.exists():
                x = (plan.stat().st_mtime - global_start) / 60.0
                print(f"warning: no samples for concurrency {concurrency}; using run-plan timestamp")
            else:
                print(f"warning: no samples for concurrency {concurrency}; marker omitted")
                continue
        for axis in (memory_axis, cpu_axis):
            axis.axvline(x, color="0.35", linestyle="--", linewidth=1)
        memory_axis.text(x, memory_max * 0.98, f"{concurrency} sessions", rotation=90,
                         va="top", ha="right", fontsize=9, color="0.25")

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=160)
    plt.close(fig)
    print(f"wrote {output}")
    print("measured stages:", ", ".join(str(stage) for stage in sorted(measured)))
    plot_usage_cdf(plotted_points, cdf_output)


def write_svg_plot(output: Path, points: list[tuple], stage_starts: dict[int, float],
                   requested_stages: list[int], subtitle: str = "",
                   guard_percent: float | None = None) -> None:
    """Render the CPU, memory, and session plot without optional packages."""
    width, height = 1500, 820
    left, right, top, bottom = 100, 190, 90, 100
    graph_w, graph_h = width - left - right, height - top - bottom
    max_x = max((p[0] for p in points), default=1) or 1
    max_sessions = max((p[3] for p in points), default=1) or 1
    session_max = max_sessions * 1.12
    colors = {"cpu": "#d97706", "memory": "#2563eb", "sessions": "#15803d"}

    def x_pos(value: float) -> float:
        return left + value / max_x * graph_w

    def y_pos(value: float, maximum: float) -> float:
        return top + graph_h - value / maximum * graph_h

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             '<style>text{font-family:DejaVu Sans,Arial,sans-serif} .small{font-size:13px}</style>',
             '<text x="100" y="42" font-size="24" font-weight="bold" fill="#111827">SWE-bench microVM scale: CPU, memory, and sessions</text>']
    for tick in range(6):
        fraction = tick / 5
        y = top + graph_h - fraction * graph_h
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left+graph_w}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{left-12}" y="{y+5:.1f}" text-anchor="end" class="small" fill="{colors["cpu"]}">{fraction*100:.0f}%</text>')
        parts.append(f'<text x="{left+graph_w+12}" y="{y+5:.1f}" class="small" fill="{colors["memory"]}">{fraction*100:.0f}%</text>')
        parts.append(f'<text x="{left+graph_w+100}" y="{y+5:.1f}" class="small" fill="{colors["sessions"]}">{fraction*session_max:.0f}</text>')
    parts.extend([f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+graph_h}" stroke="{colors["cpu"]}" stroke-width="2"/>',
                  f'<line x1="{left}" y1="{top+graph_h}" x2="{left+graph_w}" y2="{top+graph_h}" stroke="#374151" stroke-width="2"/>',
                  f'<line x1="{left+graph_w}" y1="{top}" x2="{left+graph_w}" y2="{top+graph_h}" stroke="{colors["memory"]}" stroke-width="2"/>',
                  f'<line x1="{left+graph_w+85}" y1="{top}" x2="{left+graph_w+85}" y2="{top+graph_h}" stroke="{colors["sessions"]}" stroke-width="2"/>'])
    for tick in range(6):
        value = max_x * tick / 5
        x = x_pos(value)
        parts.append(f'<text x="{x:.1f}" y="{top+graph_h+25}" text-anchor="middle" class="small" fill="#374151">{value:.1f}</text>')
    x_ticks = " ".join(f'{x_pos(p[0]):.2f},{y_pos(p[2], 100):.2f}' for p in points)
    mem_ticks = " ".join(f'{x_pos(p[0]):.2f},{y_pos(p[1], 100):.2f}' for p in points)
    session_ticks = " ".join(f'{x_pos(p[0]):.2f},{y_pos(p[3], session_max):.2f}' for p in points)
    parts.extend([f'<polyline points="{x_ticks}" fill="none" stroke="{colors["cpu"]}" stroke-width="1.5"/>',
                  f'<polyline points="{mem_ticks}" fill="none" stroke="{colors["memory"]}" stroke-width="1.5"/>',
                  f'<polyline points="{session_ticks}" fill="none" stroke="{colors["sessions"]}" stroke-width="2"/>'])
    for stage in requested_stages:
        if stage not in stage_starts:
            continue
        x = x_pos(stage_starts[stage])
        parts.append(f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{top+graph_h}" stroke="#6b7280" stroke-dasharray="4 5"/>')
        parts.append(f'<text x="{x+4:.2f}" y="{top+18}" class="small" fill="#374151" transform="rotate(90 {x+4:.2f} {top+18})">{stage} sessions</text>')
    if guard_percent is not None:
        parts.extend([f'<line x1="{left}" y1="{y_pos(guard_percent,100):.2f}" x2="{left+graph_w}" y2="{y_pos(guard_percent,100):.2f}" stroke="#991b1b" stroke-dasharray="6 5"/>',
                      f'<text x="{left+graph_w-4}" y="{y_pos(guard_percent,100)-6:.2f}" text-anchor="end" class="small" fill="#991b1b">{guard_percent:g}% node memory guard</text>'])
    parts.extend([f'<text x="{left}" y="{height-28}" class="small" fill="#374151">Minutes since experiment start</text>',
                  f'<text x="20" y="{top-14}" class="small" fill="{colors["cpu"]}">Whole-host CPU (%)</text>',
                  f'<text x="{left+graph_w+7}" y="{top-14}" class="small" fill="{colors["memory"]}">Node-0 memory (%)</text>',
                  f'<text x="{left+graph_w+94}" y="{top-14}" class="small" fill="{colors["sessions"]}">Sessions</text>',
                  f'<text x="100" y="70" class="small" fill="#4b5563">{subtitle}</text>',
                  '</svg>'])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(parts), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-root", type=Path,
                        default=Path("traces/first_remote_exp_manual3"))
    parser.add_argument("--compare-trace-root", type=Path,
                        help="overlay another run's selected concurrency stage")
    parser.add_argument("--compare-concurrency", type=int, default=32,
                        help="stage count to compare when --compare-trace-root is set")
    parser.add_argument("--trace-label", default="Node 0 only",
                        help="label for --trace-root in a two-run comparison")
    parser.add_argument("--compare-label", default="TPP enabled",
                        help="label for --compare-trace-root")
    parser.add_argument("--output", type=Path, default=None,
                        help="output image; defaults to <trace-root>/resource-usage.jpg")
    parser.add_argument("--cdf-output", type=Path, default=None,
                        help="CDF plot path; defaults beside --output with '-cdf' suffix")
    parser.add_argument("--stages", type=int, nargs="+", default=[1, 2, 4, 8, 16],
                        help="concurrency stages to mark with dashed lines")
    parser.add_argument("--bin-seconds", type=float, default=0.5,
                        help="time bin used to smooth aggregate samples")
    parser.add_argument("--cpu-capacity-cores", type=int,
                        default=os.cpu_count() or 1,
                        help="host CPU capacity used as the 100%% denominator")
    parser.add_argument("--memory-capacity-gib", type=float, default=None,
                        help="host memory capacity used as the 100%% denominator")
    args = parser.parse_args()
    output = args.output or (args.trace_root / "resource-usage.jpg")
    if args.bin_seconds <= 0:
        parser.error("--bin-seconds must be positive")
    if args.compare_trace_root:
        plot_comparison(args.trace_root, args.compare_trace_root, output, args.bin_seconds,
                        args.trace_label, args.compare_label, args.compare_concurrency)
        return
    if args.cpu_capacity_cores <= 0:
        parser.error("--cpu-capacity-cores must be positive")
    memory_capacity_gib = args.memory_capacity_gib or host_memory_gib()
    if memory_capacity_gib <= 0:
        parser.error("--memory-capacity-gib must be positive")
    cdf_output = args.cdf_output or output.with_name(
        f"{output.stem}-cdf{output.suffix}")
    plot(args.trace_root, output, args.stages, args.bin_seconds,
         args.cpu_capacity_cores, memory_capacity_gib, cdf_output)


if __name__ == "__main__":
    main()
