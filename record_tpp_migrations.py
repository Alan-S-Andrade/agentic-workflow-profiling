#!/usr/bin/env python3
"""Sample host TPP counters and per-VMM NUMA residency until a run exits."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path


VMSTAT_NAMES = (
    "pgdemote_kswapd",
    "pgdemote_direct",
    "pgdemote_file",
    "pgdemote_anon",
    "pgpromote_candidate",
    "pgpromote_candidate_demoted",
    "pgpromote_candidate_anon",
    "pgpromote_candidate_file",
    "pgpromote_tried",
    "pgpromote_file",
    "pgpromote_anon",
    "numa_pages_migrated",
    "pgmigrate_success",
)


def proc_start_ticks(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    return int(stat[stat.rfind(")") + 2 :].split()[19])


def vmstat_snapshot() -> dict[str, int | None]:
    values = {}
    for line in Path("/proc/vmstat").read_text().splitlines():
        name, _, raw = line.partition(" ")
        if name in VMSTAT_NAMES:
            values[name] = int(raw)
    return {name: values.get(name) for name in VMSTAT_NAMES}


def numa_residency(pid: int) -> dict:
    pages: dict[str, int] = defaultdict(int)
    policies: dict[str, int] = defaultdict(int)
    try:
        lines = Path(f"/proc/{pid}/numa_maps").read_text().splitlines()
    except (FileNotFoundError, PermissionError):
        return {"available": False, "pages_by_node": {}, "policy_markers": {}}
    for line in lines:
        for field in line.split():
            match = re.fullmatch(r"N(\d+)=(\d+)", field)
            if match:
                pages[f"node{match.group(1)}"] += int(match.group(2))
            elif re.match(r"^(?:bind|interleave|preferred|local):", field):
                policies[field.split(":", 1)[0]] += 1
    return {"available": True, "pages_by_node": dict(sorted(pages.items())),
            "policy_markers": dict(policies)}


def cgroup_memory_snapshot(pid: int) -> dict:
    """Read the host cgroup's per-node footprint for a VMM process."""
    try:
        rows = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        unified = next(row for row in rows if row.startswith("0::"))
        relative = unified.partition("::")[2].lstrip("/")
        cgroup = Path("/sys/fs/cgroup") / relative
        numa_lines = (cgroup / "memory.numa_stat").read_text().splitlines()
        stat_lines = (cgroup / "memory.stat").read_text().splitlines()
    except (OSError, StopIteration):
        return {"available": False, "path": None, "numa_bytes": {},
                "memory_stat_bytes": {}}

    numa_bytes = {}
    for line in numa_lines:
        fields = line.split()
        if not fields:
            continue
        per_node = {}
        for field in fields[1:]:
            match = re.fullmatch(r"N(\d+)=(\d+)", field)
            if match:
                per_node[f"node{match.group(1)}"] = int(match.group(2))
        if per_node:
            numa_bytes[fields[0]] = per_node

    selected_stat = {"anon", "file", "shmem", "active_anon", "inactive_anon",
                     "active_file", "inactive_file"}
    memory_stat_bytes = {}
    for line in stat_lines:
        name, _, raw = line.partition(" ")
        if name in selected_stat and raw:
            memory_stat_bytes[name] = int(raw)
    return {"available": True, "path": str(cgroup), "numa_bytes": numa_bytes,
            "memory_stat_bytes": memory_stat_bytes}


def vm_snapshot(vm_root: Path) -> list[dict]:
    rows = []
    # Accept either one stage's vms/ directory or an experiment root containing
    # sessions-*/vms/session-*/sandbox.json. The latter keeps sampling useful
    # across a multi-stage concurrency sweep.
    metadata_paths = sorted(vm_root.glob("session-*/sandbox.json"))
    if not metadata_paths:
        metadata_paths = sorted(vm_root.glob("sessions-*/vms/session-*/sandbox.json"))
    for metadata_path in metadata_paths:
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = int(metadata["pid"])
        rows.append({"session": int(metadata_path.parent.name.removeprefix("session-")),
                     "instance_id": metadata.get("instance_id"), "pid": pid,
                     "residency": numa_residency(pid),
                     "host_cgroup_memory": cgroup_memory_snapshot(pid)})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-pid", type=int, required=True,
                        help="PID of the experiment runner; sampling ends when it exits")
    parser.add_argument("--vm-root", type=Path, required=True,
                        help="sessions-*/vms directory containing sandbox.json files")
    parser.add_argument("--output", type=Path, required=True,
                        help="JSONL output path for host counters and per-VM residency")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="sampling interval in seconds (default: 5)")
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    target_start = proc_start_ticks(args.run_pid)
    if target_start is None:
        parser.error(f"runner PID {args.run_pid} is not running")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", buffering=1) as stream:
        while proc_start_ticks(args.run_pid) == target_start:
            row = {"timestamp": time.time(), "run_pid": args.run_pid,
                   "counter_scope": "host-global", "vmstat": vmstat_snapshot(),
                   "vms": vm_snapshot(args.vm_root)}
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
