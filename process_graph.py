#!/usr/bin/env python3
"""Convert opt-in strace process events into JSONL and a small DOT process tree."""

import argparse
import glob
import json
from pathlib import Path
import re


SPAWN = re.compile(r"^(?P<time>\d+\.\d+)\s+(?P<op>clone|clone3|fork|vfork)\(.*\)\s+=\s+(?P<child>\d+)")
EXEC = re.compile(r'^(?P<time>\d+\.\d+)\s+execve\("(?P<path>(?:\\.|[^"\\])*)"')
BOUNDARY = re.compile(r"^(?P<time>\d+\.\d+)\s+(?P<op>setns|unshare)\(")


def q(value: str) -> str:
    return json.dumps(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-prefix", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--dot", type=Path, required=True)
    args = parser.parse_args()
    events = []
    executables: dict[int, list[str]] = {}
    pids: set[int] = set()
    spawn_edges = []
    for filename in sorted(glob.glob(f"{args.input_prefix}.*")):
        try:
            pid = int(filename.rsplit(".", 1)[1])
        except ValueError:
            continue
        pids.add(pid)
        for line in Path(filename).read_text(encoding="utf-8", errors="replace").splitlines():
            match = SPAWN.match(line)
            if match:
                child = int(match["child"])
                pids.add(child)
                event = {"kind": "process_event", "event": "spawn", "timestamp": float(match["time"]), "pid": pid, "child_pid": child, "operation": match["op"]}
                events.append(event)
                spawn_edges.append(event)
                continue
            match = EXEC.match(line)
            if match:
                executable = bytes(match["path"], "utf-8").decode("unicode_escape")
                executables.setdefault(pid, []).append(executable)
                events.append({"kind": "process_event", "event": "exec", "timestamp": float(match["time"]), "pid": pid, "executable": executable})
                continue
            match = BOUNDARY.match(line)
            if match:
                events.append({"kind": "process_event", "event": "namespace_boundary", "timestamp": float(match["time"]), "pid": pid, "operation": match["op"]})

    args.events.parent.mkdir(parents=True, exist_ok=True)
    args.events.write_text("".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events), encoding="utf-8")
    lines = [
        "digraph process_tree {",
        "  rankdir=LR;",
        '  graph [fontname="Helvetica", fontsize=20, labelloc="t", label="Observed process lineage (strace)"];',
        '  node [fontname="Helvetica", fontsize=12, shape=box, style="rounded,filled", fillcolor="#E0F2FE", color="#0369A1"];',
        '  edge [fontname="Helvetica", fontsize=10, color="#475569"];',
    ]
    for pid in sorted(pids):
        command = executables.get(pid, ["no exec observed"])[-1]
        if len(command) > 72:
            command = "…" + command[-71:]
        label = f"PID {pid}\n{command}"
        lines.append(f"  p{pid} [label={q(label)}];")
    for edge in spawn_edges:
        lines.append(f"  p{edge['pid']} -> p{edge['child_pid']} [label={q(edge['operation'])}];")
    lines.append("}")
    args.dot.parent.mkdir(parents=True, exist_ok=True)
    args.dot.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
