#!/usr/bin/env python3
"""Measure next-burst latency after moving idle OpenHands session pages to N1.

Sessions always execute on NUMA-node-0 CPUs.  Their cgroup permits nodes 0,1
so root can migrate already-resident idle agent pages from node 0 to node 1.
The response is then served by the same resident agent-server process on node
0, making the first request an end-to-end remote-memory access measurement.
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import time
import uuid
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

NODE0_CPUS = "0,2,4,6,8,10,12,14,16,18,20,22,24,26,28,30,32,34,36,38,40,42,44,46"
IMAGE = "openhands-trace-agent-runtime:1.44"
SERVER = "exec /opt/openhands-agent/bin/python -m openhands.agent_server --host 127.0.0.1 --port 8000"


def run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, check=False, **kwargs)


def p99(values):
    values = sorted(values)
    return values[min(len(values) - 1, max(0, int(len(values) * .99 + .999999) - 1))]


def numa_pages(pid):
    result = {"N0": 0, "N1": 0}
    try:
        for line in (Path("/proc") / str(pid) / "numa_maps").read_text().splitlines():
            for key in result:
                marker = key + "="
                for token in line.split():
                    if token.startswith(marker):
                        result[key] += int(token[len(marker):])
    except OSError:
        pass
    return {key: value * os.sysconf("SC_PAGE_SIZE") for key, value in result.items()}


def process_tree(root):
    parents = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            parents[int(entry.name)] = int(stat[stat.rfind(")") + 2:].split()[1])
        except (OSError, IndexError, ValueError):
            continue
    result, changed = {root}, True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in result and pid not in result:
                result.add(pid)
                changed = True
    return sorted(result)


def numa_pages_for_pids(pids):
    totals = {"N0": 0, "N1": 0}
    for pid in pids:
        pages = numa_pages(pid)
        for node, value in pages.items():
            totals[node] += value
    return totals


def inspect_pid(container):
    value = run(["docker", "inspect", "--format", "{{.State.Pid}}", container]).stdout.strip()
    return int(value) if value.isdigit() else 0


def inspect_pids(container):
    root = inspect_pid(container)
    return process_tree(root) if root else []


def burst(container):
    started = time.perf_counter_ns()
    result = run(["docker", "exec", container, "curl", "-fsS", "--max-time", "10", "http://127.0.0.1:8000/server_info"])
    return (time.perf_counter_ns() - started) / 1e6, result.returncode, result.stderr[-500:]


def wait_ready(container, deadline):
    while time.monotonic() < deadline:
        _, status, _ = burst(container)
        if status == 0:
            return True
        time.sleep(.25)
    return False


def render(output, rows):
    xs = [row["sessions"] for row in rows]
    local = [row["local_p99_ms"] for row in rows]
    remote = [row["remote_p99_ms"] for row in rows]
    fig, axis = plt.subplots(figsize=(10, 6), layout="constrained")
    axis.plot(xs, local, color="#2563eb", marker="o", linewidth=2.2, label="Local N0")
    axis.plot(xs, remote, color="#dc2626", marker="o", linewidth=2.2, label="Migrated N1")
    axis.set_title("Idle LRU Migration: Next-Burst P99 Latency")
    axis.set_xlabel("Resident sessions")
    axis.set_ylabel("P99 latency (ms)")
    axis.set_xticks(xs)
    axis.set_ylim(bottom=0)
    axis.grid(True, color="#d1d5db", linewidth=0.7, alpha=0.8)
    axis.legend(frameon=False)
    fig.savefig(output / "numa-idle-lru-p99.png", dpi=200)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--levels", default="1,4,8,16,32,48")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--idle-seconds", type=float, default=2)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run as root so migratepages can move shared process pages")
    if args.output.exists():
        raise SystemExit(f"output exists: {args.output}")
    levels = [int(x) for x in args.levels.split(",")]
    args.output.mkdir(parents=True)
    label = "openhands.numa-latency=" + uuid.uuid4().hex
    all_rows = []
    numa_balancing = Path("/proc/sys/kernel/numa_balancing")
    original_balancing = numa_balancing.read_text().strip()
    # Remote placement must survive until the immediately following burst.
    # Restore the host policy in finally, including on failed experiments.
    numa_balancing.write_text("0\n")
    try:
      for level in levels:
        sessions = []
        for index in range(level):
            workspace = args.output / f"workspace-{level:03d}-{index:03d}"
            shutil.copytree(args.source_workspace, workspace, symlinks=True)
            created = run(["docker", "run", "-d", "--rm", "--init", "--network", "none", "--workdir", "/workspace",
                           "--label", label, "--cpuset-cpus", NODE0_CPUS, "--cpuset-mems", "0,1",
                           "--mount", f"type=bind,src={workspace.resolve()},dst=/workspace", IMAGE,
                           "bash", "-lc", SERVER])
            if created.returncode:
                raise RuntimeError(created.stderr)
            sessions.append((index, created.stdout.strip(), workspace))
        try:
            deadline = time.monotonic() + 90
            if not all(wait_ready(container, deadline) for _, container, _ in sessions):
                raise RuntimeError("agent server did not become ready")
            # The initial requests warm framework pages locally on node 0.
            local = [burst(container)[0] for _, container, _ in sessions for _ in range(args.repeats)]
            migrations, remote_records = [], []
            # Every remote timing is the first burst after a fresh migration.
            # This prevents later repeats from silently measuring pages that
            # were already pulled into node-0 cache/local placement.
            for cycle in range(args.repeats):
                time.sleep(args.idle_seconds)
                before = {container: numa_pages_for_pids(inspect_pids(container)) for _, container, _ in sessions}
                cycle_migrations = {}
                # Oldest-created session is the LRU tie-breaker; migrate every
                # idle session so P99 represents a complete idle pool.
                for _, container, _ in sessions:
                    pids = inspect_pids(container)
                    moves = [run(["migratepages", str(pid), "0", "1"]) for pid in pids]
                    cycle_migrations[container] = {"pids": pids,
                                                   "returncodes": [move.returncode for move in moves],
                                                   "stdout": [move.stdout for move in moves],
                                                   "stderr": [move.stderr for move in moves],
                                                   "before": before[container],
                                                   "after": numa_pages_for_pids(pids)}
                migrations.append(cycle_migrations)
                remote_records.extend(burst(container) for _, container, _ in sessions)
            remote = [value for value, status, _ in remote_records if status == 0]
            if not remote:
                raise RuntimeError("all remote bursts failed")
            row = {"sessions": level, "local_p99_ms": p99(local), "remote_p99_ms": p99(remote),
                   "local_median_ms": statistics.median(local), "remote_median_ms": statistics.median(remote),
                   "migration": migrations, "remote_failures": [error for _, status, error in remote_records if status]}
            all_rows.append(row)
            (args.output / "levels.jsonl").open("a").write(json.dumps(row) + "\n")
        finally:
            for _, container, workspace in sessions:
                run(["docker", "rm", "-f", container])
                shutil.rmtree(workspace, ignore_errors=True)
    finally:
        numa_balancing.write_text(original_balancing + "\n")
    (args.output / "summary.json").write_text(json.dumps(all_rows, indent=2) + "\n")
    render(args.output, all_rows)
    print(args.output / "numa-idle-lru-p99.png")


if __name__ == "__main__":
    main()
