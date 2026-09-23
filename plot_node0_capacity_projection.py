#!/usr/bin/env python3
"""Render measured and projected node-0 agent-session capacity on three axes."""

import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


GIB = 2**30
NODE0_GIB = 128809 / 1024
THRESHOLD_GIB = NODE0_GIB * 0.85


def main():
    experiment = Path("traces/openhands-node0-agent-runtime-20260918T151500Z-final")
    rows = [json.loads(line) for line in (experiment / "samples.jsonl").read_text().splitlines()]
    data = [(x["elapsed_ms"] / 60_000, x["sandbox_pid_count"] / 2,
             x["docker_memory_current_bytes"] / GIB, x.get("capacity_cpu_percent"))
            for x in rows if x.get("docker_memory_current_bytes") and x.get("sandbox_pid_count", 0) >= 20]
    if len(data) < 2:
        raise SystemExit("insufficient live cgroup samples")

    n = len(data)
    sx, sy = sum(x[1] for x in data), sum(x[2] for x in data)
    sxx, sxy = sum(x[1] ** 2 for x in data), sum(x[1] * x[2] for x in data)
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    intercept = (sy - slope * sx) / n
    capacity_sessions = (THRESHOLD_GIB - intercept) / slope
    first, last = data[0], data[-1]
    launch_rate = (last[1] - first[1]) / max(last[0] - first[0], 0.01)
    projected_end = last[0] + (capacity_sessions - last[1]) / launch_rate
    projected_cpu = statistics.median([x[3] for x in data[-200:] if x[3] is not None])

    observed_time = [x[0] for x in data]
    observed_sessions = [x[1] for x in data]
    observed_memory = [x[2] for x in data]
    observed_cpu = [x[3] for x in data]
    projected_time = [last[0], projected_end]

    fig, memory_axis = plt.subplots(figsize=(12, 7), layout="constrained")
    cpu_axis = memory_axis.twinx()
    sessions_axis = memory_axis.twinx()
    sessions_axis.spines.right.set_position(("outward", 62))

    memory_axis.plot(observed_time, observed_memory, color="#2563eb", linewidth=2.2, label="Memory")
    memory_axis.plot(projected_time, [last[2], THRESHOLD_GIB], color="#2563eb", linestyle="--", linewidth=2.2)
    memory_axis.axhline(THRESHOLD_GIB, color="#991b1b", linestyle=":", linewidth=1.4, label="85% DRAM")
    cpu_axis.plot(observed_time, observed_cpu, color="#d97706", linewidth=1.8, label="CPU")
    cpu_axis.plot(projected_time, [projected_cpu, projected_cpu], color="#d97706", linestyle="--", linewidth=1.8)
    sessions_axis.plot(observed_time, observed_sessions, color="#15803d", linewidth=2.2, label="Sessions")
    sessions_axis.plot(projected_time, [last[1], capacity_sessions], color="#15803d", linestyle="--", linewidth=2.2)

    memory_axis.set_title("Node-0 Session Memory Capacity Projection")
    memory_axis.set_xlabel("Elapsed time (min)")
    memory_axis.set_ylabel("Docker cgroup memory (GiB)", color="#2563eb")
    cpu_axis.set_ylabel("Node-0 CPU (%)", color="#d97706")
    sessions_axis.set_ylabel("Resident sessions", color="#15803d")
    memory_axis.set_xlim(0, projected_end * 1.04)
    memory_axis.set_ylim(bottom=0)
    cpu_axis.set_ylim(0, 100)
    sessions_axis.set_ylim(bottom=0)
    memory_axis.grid(True, color="#d1d5db", linewidth=0.7, alpha=0.8)
    memory_axis.tick_params(axis="y", colors="#2563eb")
    cpu_axis.tick_params(axis="y", colors="#d97706")
    sessions_axis.tick_params(axis="y", colors="#15803d")
    handles, labels = [], []
    for axis in (memory_axis, cpu_axis, sessions_axis):
        h, l = axis.get_legend_handles_labels()
        handles.extend(h)
        labels.extend(l)
    memory_axis.legend(handles, labels, loc="upper left", frameon=False, ncol=4)
    fig.savefig("node0-memory-exhaustion-projection.png", dpi=200)
    plt.close(fig)

    Path("node0-memory-exhaustion-projection.json").write_text(json.dumps({
        "observed_end_min": last[0], "memory_fit_intercept_gib": intercept,
        "memory_fit_gib_per_session": slope, "capacity_sessions": capacity_sessions,
        "node0_threshold_gib": THRESHOLD_GIB, "session_launch_rate_per_min": launch_rate,
        "projected_exhaustion_min": projected_end, "projected_cpu_percent": projected_cpu,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
