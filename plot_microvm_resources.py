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

import matplotlib.pyplot as plt

STAGE_RE = re.compile(r"sessions-(\d+)$")


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open() as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def stage_points(stage_dir: Path, bin_seconds: float,
                 cpu_capacity_cores: int, memory_capacity_gib: float):
    host_sample_file = stage_dir / "host-samples.jsonl"
    if host_sample_file.exists():
        rows = read_jsonl(host_sample_file)
        if not rows:
            raise ValueError(f"no host samples in {stage_dir}")
        stage_start = min(float(row["timestamp"]) for row in rows)
        latest: dict[float, dict] = {}
        for row in rows:
            timestamp = float(row["timestamp"])
            bucket = math.floor((timestamp - stage_start) / bin_seconds) * bin_seconds
            latest[bucket] = row
        points = []
        previous = None
        for timestamp in sorted(latest):
            row = latest[timestamp]
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
            points.append((timestamp, memory_percent, cpu_percent))
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
                       100.0 * sum(value[1] for value in values) / cpu_capacity_cores))
    return stage_start, points


def discover_stages(trace_root: Path) -> dict[int, Path]:
    stages = {}
    for path in trace_root.glob("sessions-*"):
        match = STAGE_RE.fullmatch(path.name)
        if match:
            stages[int(match.group(1))] = path
    return stages


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
                    stage_dir, bin_seconds, cpu_capacity_cores, memory_capacity_gib)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"warning: skipping {stage_dir}: {exc}")
    if not measured:
        raise SystemExit(f"no session-samples.jsonl files found under {trace_root}")

    global_start = min(start for start, _ in measured.values())
    stage_starts = {stage: (start - global_start) / 60.0
                    for stage, (start, _) in measured.items()}
    plotted_points = []
    for stage, (_, points) in measured.items():
        offset = stage_starts[stage]
        minutes = [offset + relative / 60.0 for relative, _, _ in points]
        memory = [memory for _, memory, _ in points]
        cpu = [cpu for _, _, cpu in points]
        plotted_points.extend(zip(minutes, memory, cpu))
    plotted_points.sort(key=lambda point: point[0])

    fig, memory_axis = plt.subplots(figsize=(12, 6), constrained_layout=True)
    cpu_axis = memory_axis.twinx()
    minutes = [point[0] for point in plotted_points]
    memory_percent = [point[1] for point in plotted_points]
    memory_axis.plot(minutes, memory_percent, color="tab:blue", linewidth=1.5,
                     label="Memory")
    cpu_values = [point[2] for point in plotted_points]
    cpu_axis.plot(minutes, cpu_values, color="tab:orange", linewidth=1.5,
                  label="System-wide CPU utilization")
    cpu_label = "System-wide CPU utilization (%)"
    memory_axis.set_ylabel("Memory utilization (%)", color="tab:blue")
    memory_axis.tick_params(axis="y", labelcolor="tab:blue")
    cpu_axis.set_ylabel(cpu_label, color="tab:orange")
    cpu_axis.tick_params(axis="y", labelcolor="tab:orange")
    memory_axis.set_xlabel("Minutes since experiment start")
    memory_axis.set_title("SWE-bench microVM resource usage")
    memory_axis.grid(True, alpha=0.25)
    cpu_max = max(100.0, max(cpu_values, default=0.0) * 1.05)
    cpu_axis.set_ylim(0, cpu_max)
    memory_axis.legend(loc="upper left")
    cpu_axis.legend(loc="upper right")

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-root", type=Path,
                        default=Path("traces/first_remote_exp_manual3"))
    parser.add_argument("--output", type=Path,
                        default=Path("traces/first_remote_exp_manual3/resource-usage.png"))
    parser.add_argument("--cdf-output", type=Path, default=None,
                        help="CDF plot path; defaults beside --output with '-cdf' suffix")
    parser.add_argument("--stages", type=int, nargs="+", default=[1, 2, 4, 8],
                        help="concurrency stages to mark with dashed lines")
    parser.add_argument("--bin-seconds", type=float, default=0.5,
                        help="time bin used to smooth aggregate samples")
    parser.add_argument("--cpu-capacity-cores", type=int,
                        default=os.cpu_count() or 1,
                        help="host CPU capacity used as the 100%% denominator")
    parser.add_argument("--memory-capacity-gib", type=float, default=None,
                        help="host memory capacity used as the 100%% denominator")
    args = parser.parse_args()
    if args.bin_seconds <= 0:
        parser.error("--bin-seconds must be positive")
    if args.cpu_capacity_cores <= 0:
        parser.error("--cpu-capacity-cores must be positive")
    memory_capacity_gib = args.memory_capacity_gib or host_memory_gib()
    if memory_capacity_gib <= 0:
        parser.error("--memory-capacity-gib must be positive")
    cdf_output = args.cdf_output or args.output.with_name(
        f"{args.output.stem}-cdf{args.output.suffix}")
    plot(args.trace_root, args.output, args.stages, args.bin_seconds,
         args.cpu_capacity_cores, memory_capacity_gib, cdf_output)


if __name__ == "__main__":
    main()
