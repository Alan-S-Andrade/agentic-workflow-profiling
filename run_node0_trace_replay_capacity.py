#!/usr/bin/env python3
"""Measure node-0 capacity with persistent, no-LLM OpenHands trace replays.

Each resident session receives one private workspace and one persistent Docker
container.  The recorded OpenHands terminal actions run through ``docker exec``
in that container.  Recorded LLM spans are waits only: no provider request is
made.  The experiment stops before continuing past the requested aggregate
Docker-cgroup memory threshold and removes only resources carrying its unique
Docker label.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


GIB = 2**30
NODE0_CPUS = "0,2,4,6,8,10,12,14,16,18,20,22,24,26,28,30,32,34,36,38,40,42,44,46"
AGENT_SERVER = (
    "export PYTHONDONTWRITEBYTECODE=1; exec /opt/openhands-agent/bin/python -m openhands.agent_server "
    "--host 127.0.0.1 --port 8000"
)
RUNNING = True


def stop(*_args):
    global RUNNING
    RUNNING = False


def run(command):
    return subprocess.run(command, text=True, capture_output=True, check=False)


def node_memory_total(node: int) -> int:
    path = Path(f"/sys/devices/system/node/node{node}/meminfo")
    if not path.exists():
        raise SystemExit(f"NUMA node {node} does not exist")
    for line in path.read_text().splitlines():
        if "MemTotal:" in line:
            match = re.search(r"(\d+)\s+kB", line)
            if match:
                return int(match.group(1)) * 1024
    raise SystemExit(f"could not read node-{node} memory capacity")


def cpu_ticks(cpus: list[int]) -> tuple[int, int]:
    wanted = {f"cpu{cpu}" for cpu in cpus}
    total = busy = 0
    for line in Path("/proc/stat").read_text().splitlines():
        fields = line.split()
        if fields and fields[0] in wanted:
            ticks = [int(value) for value in fields[1:]]
            total += sum(ticks)
            busy += sum(ticks[:3])
    return total, busy


def parse_cpus(value: str) -> list[int]:
    result = []
    for item in value.split(","):
        if "-" in item:
            start, end = (int(part) for part in item.split("-", 1))
            result.extend(range(start, end + 1))
        else:
            result.append(int(item))
    return sorted(set(result))


def labelled_containers(label: str) -> list[tuple[str, int]]:
    listed = run(["docker", "ps", "-q", "--filter", f"label={label}"])
    ids = listed.stdout.split()
    if not ids:
        return []
    inspected = run(["docker", "inspect", "--format", "{{.Id}} {{.State.Pid}}", *ids])
    result = []
    for line in inspected.stdout.splitlines():
        container, pid, *extra = line.split()
        if not extra and pid.isdigit() and int(pid) > 0:
            result.append((container, int(pid)))
    return result


def cgroup_memory(pid: int) -> dict[str, int] | None:
    try:
        relative = next(line.split("::", 1)[1] for line in
                        (Path("/proc") / str(pid) / "cgroup").read_text().splitlines()
                        if line.startswith("0::"))
        root = Path("/sys/fs/cgroup") / relative.lstrip("/")
        memory = {key: int(value) for key, value in
                  (line.split() for line in (root / "memory.stat").read_text().splitlines())}
        return {
            "current": int((root / "memory.current").read_text()),
            "anon": memory.get("anon", 0), "file": memory.get("file", 0),
            "slab": memory.get("slab", 0),
        }
    except (FileNotFoundError, OSError, StopIteration, ValueError):
        return None


def aggregate_memory(containers: list[tuple[str, int]]) -> dict[str, int]:
    total = {"current": 0, "anon": 0, "file": 0, "slab": 0}
    for _, pid in containers:
        stats = cgroup_memory(pid)
        if stats:
            for key, value in stats.items():
                total[key] += value
    return total


def wait_for_agent_server(container: str, deadline: float) -> bool:
    """Do not sample an image layer before the resident agent framework loads."""
    while time.monotonic() < deadline:
        ready = run(["docker", "exec", container, "curl", "-fsS", "--max-time", "2",
                     "http://127.0.0.1:8000/server_info"])
        if ready.returncode == 0:
            return True
        time.sleep(0.1)
    return False


def expected_step_count(trace_dir: Path) -> int:
    """Count recorded terminal actions plus captured LLM spans to replay."""
    terminal = 0
    for line in (trace_dir / "agent-events.jsonl").read_text().splitlines():
        event = json.loads(line).get("event", {})
        if (event.get("action") or {}).get("kind") == "TerminalAction":
            terminal += 1
    llm = sum(1 for line in (trace_dir / "llm.jsonl").read_text().splitlines() if line.strip())
    return terminal + llm


def wait_for_replay_steps(outputs: list[Path], expected: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        complete = True
        for output in outputs:
            steps = output / "steps.jsonl"
            count = sum(1 for _ in steps.open()) if steps.exists() else 0
            if count < expected:
                complete = False
                break
        if complete:
            return True
        time.sleep(0.1)
    return False


def sample(containers: list[tuple[str, int]], cpus: list[int], window_seconds: float) -> dict:
    before_total, before_busy = cpu_ticks(cpus)
    if window_seconds:
        time.sleep(window_seconds)
    after_total, after_busy = cpu_ticks(cpus)
    total_delta, busy_delta = after_total - before_total, after_busy - before_busy
    memory = aggregate_memory(containers)
    return {
        "resident_sessions": len(containers),
        "docker_cgroup_memory_bytes": memory["current"],
        "docker_cgroup_anon_bytes": memory["anon"],
        "docker_cgroup_file_bytes": memory["file"],
        "docker_cgroup_slab_bytes": memory["slab"],
        "node0_cpu_percent": (busy_delta / total_delta * 100) if total_delta else 0.0,
        "sample_window_ms": round(window_seconds * 1000, 3),
    }


def render(output: Path, rows: list[dict], threshold_bytes: int) -> None:
    sessions = [row["resident_sessions"] for row in rows]
    memory = [row["docker_cgroup_memory_bytes"] / GIB for row in rows]
    cpu = [row["node0_cpu_percent"] for row in rows]
    fig, memory_axis = plt.subplots(figsize=(11, 6.5), layout="constrained")
    cpu_axis = memory_axis.twinx()
    memory_axis.plot(sessions, memory, color="#2563eb", marker="o", linewidth=2.1,
                     markersize=4, label="Docker cgroup memory")
    memory_axis.axhline(threshold_bytes / GIB, color="#991b1b", linestyle=":",
                        linewidth=1.5, label="90% memory stop")
    cpu_axis.plot(sessions, cpu, color="#d97706", marker="o", linewidth=1.9,
                  markersize=4, label="Node-0 CPU")
    memory_axis.set_title("Node-0 Persistent Trace-Replay Capacity")
    memory_axis.set_xlabel("Resident sessions")
    memory_axis.set_ylabel("Docker cgroup memory (GiB)", color="#2563eb")
    cpu_axis.set_ylabel("Node-0 CPU (%)", color="#d97706")
    memory_axis.set_xlim(left=0)
    memory_axis.set_ylim(bottom=0)
    cpu_axis.set_ylim(0, 100)
    memory_axis.grid(True, color="#d1d5db", linewidth=0.7, alpha=0.8)
    memory_axis.tick_params(axis="y", colors="#2563eb")
    cpu_axis.tick_params(axis="y", colors="#d97706")
    handles, labels = [], []
    for axis in (memory_axis, cpu_axis):
        handle, label = axis.get_legend_handles_labels()
        handles.extend(handle)
        labels.extend(label)
    memory_axis.legend(handles, labels, loc="upper left", frameon=False, ncol=3)
    fig.savefig(output / "node0-trace-replay-capacity.png", dpi=200)
    plt.close(fig)


def cleanup(label: str, replicas: list[dict], keep_workspaces: bool) -> None:
    for replica in replicas:
        process = replica["process"]
        if process.poll() is None:
            process.terminate()
    for replica in replicas:
        process = replica["process"]
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    ids = [container for container, _ in labelled_containers(label)]
    if ids:
        run(["docker", "rm", "-f", *ids])
    if not keep_workspaces:
        for replica in replicas:
            shutil.rmtree(replica["workspace"], ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_dir", type=Path)
    parser.add_argument("--source-workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-sessions", type=int, default=512)
    parser.add_argument("--sessions-per-point", type=int, default=4,
                        help="admit this many sessions concurrently before each plotted sample")
    parser.add_argument("--memory-stop-percent", type=float, default=90.0)
    parser.add_argument("--sample-window-seconds", type=float, default=1.0)
    parser.add_argument("--trace-completion-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--active-replay", action="store_true",
                        help="loop each trace with recorded LLM waits and sample while sessions are executing")
    parser.add_argument("--active-warmup-seconds", type=float, default=5.0,
                        help="replay time to allow before active CPU sampling")
    parser.add_argument("--replay-speed", type=float, default=1.0,
                        help="recorded LLM-wait multiplier; 0 skips waits without any LLM call")
    parser.add_argument("--idle-hold-seconds", type=float, default=86400.0)
    parser.add_argument("--image", default="openhands-trace-agent-runtime:1.44")
    parser.add_argument("--cpuset-cpus", default=NODE0_CPUS)
    parser.add_argument("--cpuset-mems", default="0")
    parser.add_argument("--cache-tmpfs-size", default="768m",
                        help="tmpfs size for each session's /root/.cache; use 0 to retain overlay storage")
    parser.add_argument("--local-tmpfs-size", default="0",
                        help="tmpfs size for each session's /root/.local; use 0 to retain overlay storage")
    parser.add_argument("--keep-workspaces", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run as root (for Docker cgroup accounting)")
    if not args.trace_dir.is_dir() or not args.source_workspace.is_dir():
        raise SystemExit("trace directory and source workspace must exist")
    if (args.output.exists() or args.max_sessions < 1 or args.sessions_per_point < 1
            or not 0 < args.memory_stop_percent <= 100):
        raise SystemExit("output must not exist; session counts must be positive; memory stop must be in (0, 100]")
    if args.replay_speed < 0 or args.sample_window_seconds < 0:
        raise SystemExit("replay speed and sample window must be non-negative")
    if args.active_warmup_seconds < 0:
        raise SystemExit("active warmup must be non-negative")

    cpus = parse_cpus(args.cpuset_cpus)
    steps_per_replay = expected_step_count(args.trace_dir)
    capacity_bytes = node_memory_total(0)
    threshold_bytes = int(capacity_bytes * args.memory_stop_percent / 100)
    args.output.mkdir(parents=True)
    label = "openhands.node0.capacity=" + uuid.uuid4().hex
    replicas, rows, stop_reason = [], [], "max_sessions"
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    started = time.time()
    try:
        for batch_first in range(1, args.max_sessions + 1, args.sessions_per_point):
            if not RUNNING:
                stop_reason = "signal"
                break
            containers = labelled_containers(label)
            known_containers = {container for container, _ in containers}
            if aggregate_memory(containers)["current"] >= threshold_bytes:
                stop_reason = "90_percent_docker_cgroup_memory"
                break
            batch_last = min(args.max_sessions, batch_first + args.sessions_per_point - 1)
            startup_deadline = time.monotonic() + 90
            for index in range(batch_first, batch_last + 1):
                workspace = args.output / f"workspace-{index:04d}"
                shutil.copytree(args.source_workspace, workspace, symlinks=True)
                replay_output = args.output / f"replay-{index:04d}"
                log = (args.output / f"replay-{index:04d}.log").open("w")
                command = [
                    sys.executable, str(Path(__file__).with_name("replay_openhands_trace.py")),
                    str(args.trace_dir), "--workspace", str(workspace), "--output", str(replay_output),
                    "--runtime", "docker", "--docker-image", args.image, "--container-label", label,
                    "--cpuset-cpus", args.cpuset_cpus, "--cpuset-mems", args.cpuset_mems,
                "--sandbox-command", AGENT_SERVER, "--speed", str(args.replay_speed),
                "--idle-hold-seconds", str(args.idle_hold_seconds),
            ]
                if args.cache_tmpfs_size != "0":
                    command += ["--tmpfs", f"/root/.cache:rw,size={args.cache_tmpfs_size}"]
                if args.local_tmpfs_size != "0":
                    command += ["--tmpfs", f"/root/.local:rw,size={args.local_tmpfs_size}"]
                if args.active_replay:
                    command += ["--loop"]
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, text=True)
                replicas.append({"index": index, "process": process, "workspace": workspace,
                                 "replay_output": replay_output, "log": log})
            while time.monotonic() < startup_deadline:
                containers = labelled_containers(label)
                if len(containers) >= batch_last:
                    break
                time.sleep(0.05)
            if len(containers) < batch_last:
                raise RuntimeError(f"sessions {batch_first}-{batch_last} did not create containers")
            new_containers = [container for container, _ in containers if container not in known_containers]
            if len(new_containers) != batch_last - batch_first + 1:
                raise RuntimeError(f"could not identify containers for sessions {batch_first}-{batch_last}")
            if not all(wait_for_agent_server(container, startup_deadline) for container in new_containers):
                raise RuntimeError(f"sessions {batch_first}-{batch_last} agent servers did not become ready")
            if args.active_replay:
                time.sleep(args.active_warmup_seconds)
            else:
                completion_deadline = time.monotonic() + args.trace_completion_timeout_seconds
                new_outputs = [replica["replay_output"] for replica in replicas
                               if batch_first <= replica["index"] <= batch_last]
                if not wait_for_replay_steps(new_outputs, steps_per_replay, completion_deadline):
                    raise RuntimeError(f"sessions {batch_first}-{batch_last} did not finish their recorded trace steps")
            row = sample(containers, cpus, args.sample_window_seconds)
            row.update({"session_index": batch_last, "observed_at": time.time()})
            rows.append(row)
            with (args.output / "samples.jsonl").open("a") as stream:
                stream.write(json.dumps(row) + "\n")
            print(f"sessions={row['resident_sessions']} memory_gib={row['docker_cgroup_memory_bytes'] / GIB:.3f} node0_cpu={row['node0_cpu_percent']:.2f}%", flush=True)
            if row["docker_cgroup_memory_bytes"] >= threshold_bytes:
                stop_reason = "90_percent_docker_cgroup_memory"
                break
    finally:
        for replica in replicas:
            replica["log"].close()
        if rows:
            render(args.output, rows, threshold_bytes)
        summary = {
            "started_at": started, "ended_at": time.time(), "trace_dir": str(args.trace_dir.resolve()),
            "source_workspace": str(args.source_workspace.resolve()), "image": args.image,
            "llm_mode": "recorded-duration-wait", "replay_speed": args.replay_speed,
            "active_replay": args.active_replay,
            "recorded_steps_per_replay": steps_per_replay,
            "cpuset_cpus": args.cpuset_cpus, "cpuset_mems": args.cpuset_mems,
            "cache_tmpfs_size": args.cache_tmpfs_size,
            "local_tmpfs_size": args.local_tmpfs_size,
            "node0_memory_capacity_bytes": capacity_bytes, "memory_stop_percent": args.memory_stop_percent,
            "memory_stop_bytes": threshold_bytes, "stop_reason": stop_reason,
            "resident_sessions_peak": max((row["resident_sessions"] for row in rows), default=0),
            "docker_cgroup_memory_peak_bytes": max((row["docker_cgroup_memory_bytes"] for row in rows), default=0),
            "node0_cpu_peak_percent": max((row["node0_cpu_percent"] for row in rows), default=0),
            "cleanup": "containers and copied workspaces removed" if not args.keep_workspaces else "containers removed; workspaces retained",
        }
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        cleanup(label, replicas, args.keep_workspaces)
    print(args.output / "node0-trace-replay-capacity.png")


if __name__ == "__main__":
    main()
