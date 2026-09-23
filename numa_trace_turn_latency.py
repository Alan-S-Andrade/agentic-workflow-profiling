#!/usr/bin/env python3
"""Compare local and remote NUMA memory for full captured OpenHands turns.

Both conditions execute on node-0 CPUs.  The local condition restricts each
persistent replay container to node-0 memory; the remote condition restricts
it to node-1 memory.  Thus terminal commands are issued by a node-0 host
controller against a sandbox whose resident pages are local or remote.
Recorded LLM durations are preserved as waits, never regenerated.
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


NODE0_CPUS = "0,2,4,6,8,10,12,14,16,18,20,22,24,26,28,30,32,34,36,38,40,42,44,46"
NODE0_CPU_IDS = {int(item) for item in NODE0_CPUS.split(",")}
IMAGE = "openhands-trace-agent-runtime:1.44"
SERVER = ("export PYTHONDONTWRITEBYTECODE=1; exec /opt/openhands-agent/bin/python "
          "-m openhands.agent_server --host 127.0.0.1 --port 8000")


def records(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def aggregate_turns(steps: list[dict], condition: str, repeat: int) -> list[dict]:
    grouped = defaultdict(list)
    for step in steps:
        grouped[step["turn_id"]].append(step)
    rows = []
    for turn_id, turn_steps in sorted(grouped.items()):
        terminal = sum(step["duration_ms"] for step in turn_steps if step["kind"] == "terminal")
        wait = sum(step["duration_ms"] for step in turn_steps if step["kind"] == "llm_wait")
        started = min(step["started_at"] for step in turn_steps)
        ended = max(step["ended_at"] for step in turn_steps)
        rows.append({
            "condition": condition, "repeat": repeat, "turn_id": turn_id,
            "terminal_ms": terminal, "mocked_llm_wait_ms": wait,
            "turn_wall_ms": (ended - started) * 1000,
            "terminal_calls": sum(step["kind"] == "terminal" for step in turn_steps),
        })
    return rows


def render(output: Path, rows: list[dict]) -> None:
    by_condition = defaultdict(lambda: defaultdict(list))
    for row in rows:
        by_condition[row["condition"]][row["turn_id"]].append(row["terminal_ms"])
    turns = sorted(by_condition["local_n0"])
    fig, axis = plt.subplots(figsize=(11, 6.5), layout="constrained")
    for condition, color, label in (("local_n0", "#2563eb", "Local N0 memory"),
                                    ("remote_n1", "#dc2626", "Remote N1 memory")):
        values = [statistics.median(by_condition[condition][turn]) for turn in turns]
        axis.plot(turns, values, color=color, marker="o", linewidth=2.0, label=label)
    axis.set_title("OpenHands Trace Replay: Terminal Time by NUMA Placement")
    axis.set_xlabel("Turn")
    axis.set_ylabel("Terminal execution time (ms)")
    axis.set_xticks(turns)
    axis.set_ylim(bottom=0)
    axis.grid(True, color="#d1d5db", linewidth=0.7, alpha=0.8)
    axis.legend(frameon=False)
    fig.savefig(output / "numa-trace-turn-terminal-latency.png", dpi=200)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_dir", type=Path)
    parser.add_argument("--source-workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--replay-speed", type=float, default=1.0,
                        help="1 preserves captured LLM waits; 0 omits them")
    parser.add_argument("--image", default=IMAGE)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run as root so Docker cgroup placement is inspectable")
    if args.output.exists() or args.repeats < 1 or args.replay_speed < 0:
        raise SystemExit("output must not exist; repeats must be positive; replay speed must be non-negative")
    if not args.trace_dir.is_dir() or not args.source_workspace.is_dir():
        raise SystemExit("trace directory and source workspace must exist")

    # The Docker exec controller itself runs on node-0 CPUs too.
    os.sched_setaffinity(0, NODE0_CPU_IDS)
    args.output.mkdir(parents=True)
    all_rows = []
    started = time.time()
    try:
        for condition, memory_node in (("local_n0", "0"), ("remote_n1", "1")):
            for repeat in range(1, args.repeats + 1):
                workspace = args.output / f"workspace-{condition}-{repeat:02d}"
                replay_output = args.output / f"replay-{condition}-{repeat:02d}"
                shutil.copytree(args.source_workspace, workspace, symlinks=True)
                command = [
                    sys.executable, str(Path(__file__).with_name("replay_openhands_trace.py")),
                    str(args.trace_dir), "--workspace", str(workspace), "--output", str(replay_output),
                    "--runtime", "docker", "--docker-image", args.image,
                    "--cpuset-cpus", NODE0_CPUS, "--cpuset-mems", memory_node,
                    "--sandbox-command", SERVER, "--speed", str(args.replay_speed),
                    "--tmpfs", "/root/.cache:rw,size=768m",
                ]
                completed = subprocess.run(command, text=True, capture_output=True, check=False)
                if completed.returncode:
                    raise RuntimeError(f"{condition} repeat {repeat} failed: {completed.stderr[-1000:]}")
                turn_rows = aggregate_turns(records(replay_output / "steps.jsonl"), condition, repeat)
                all_rows.extend(turn_rows)
                with (args.output / "turns.jsonl").open("a") as stream:
                    for row in turn_rows:
                        stream.write(json.dumps(row) + "\n")
                print(f"{condition} repeat={repeat} complete", flush=True)
    finally:
        for workspace in args.output.glob("workspace-*"):
            shutil.rmtree(workspace, ignore_errors=True)

    by_condition = defaultdict(list)
    for row in all_rows:
        by_condition[row["condition"]].append(row["terminal_ms"])
    summary = {
        "started_at": started, "ended_at": time.time(), "trace_dir": str(args.trace_dir.resolve()),
        "source_workspace": str(args.source_workspace.resolve()), "repeats": args.repeats,
        "replay_speed": args.replay_speed, "cpus": NODE0_CPUS,
        "memory_nodes": {"local_n0": "0", "remote_n1": "1"},
        "terminal_ms_median": {key: statistics.median(value) for key, value in by_condition.items()},
        "terminal_ms_remote_minus_local_median": (
            statistics.median(by_condition["remote_n1"]) - statistics.median(by_condition["local_n0"]) if all_rows else None
        ),
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    render(args.output, all_rows)
    print(args.output / "numa-trace-turn-terminal-latency.png")


if __name__ == "__main__":
    main()
