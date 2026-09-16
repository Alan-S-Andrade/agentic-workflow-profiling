#!/usr/bin/env python3
"""Sample a root process tree from /proc into JSONL without elevated privileges."""

import argparse
import json
import os
from pathlib import Path
import signal
import time


running = True


def stop(*_args) -> None:
    global running
    running = False


def read_pss(pid: int) -> int | None:
    """Return proportional resident bytes for one already-selected process."""
    try:
        rollup = (Path("/proc") / str(pid) / "smaps_rollup").read_text()
        pss_kib = next((int(line.split()[1]) for line in rollup.splitlines() if line.startswith("Pss:")), None)
        return pss_kib * 1024 if pss_kib is not None else None
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError):
        return None


def read_process(pid: int) -> dict | None:
    proc = Path("/proc") / str(pid)
    try:
        raw_stat = (proc / "stat").read_text()
        close = raw_stat.rfind(")")
        fields = raw_stat[close + 2 :].split()
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        cgroup = (proc / "cgroup").read_text().strip()
        ns = {name: os.readlink(proc / "ns" / name) for name in ("pid", "mnt", "net", "user", "cgroup")}
    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError, OSError):
        return None
    return {
        "pid": pid,
        "ppid": int(fields[1]),
        "state": fields[0],
        "utime_ticks": int(fields[11]),
        "stime_ticks": int(fields[12]),
        "start_ticks": int(fields[19]),
        # /proc/<pid>/stat field 39 (zero-based offset 36 after the command)
        # is the CPU on which this task was last scheduled.
        "cpu_number": int(fields[36]),
        "pss_bytes": None,
        "cmdline": cmdline,
        "cgroup": cgroup,
        "namespaces": ns,
    }


def discover_descendants(known_pids: set[int]) -> set[int]:
    """Find descendants using only the cheap stat file, off the fast path."""
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw_stat = (entry / "stat").read_text()
            close = raw_stat.rfind(")")
            parents[int(entry.name)] = int(raw_stat[close + 2 :].split()[1])
        except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError, OSError):
            continue
    expanded = set(known_pids)
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in expanded and pid not in expanded:
                expanded.add(pid)
                changed = True
    return expanded


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-pid", type=int, action="append", required=True,
                        help="Root PID to include; repeat for agent server, frontend, and proxy")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--pss-interval", type=float, default=0.25)
    parser.add_argument("--discovery-interval", type=float, default=0.25,
                        help="How often to scan /proc for newly spawned descendants")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    tick_hz = os.sysconf("SC_CLK_TCK")
    known_pids = set(args.root_pid)
    seen: set[tuple[int, int]] = set()
    active: dict[tuple[int, int], dict] = {}
    next_pss_at = 0.0
    next_discovery_at = 0.0
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("a", encoding="utf-8") as stream:
        while running:
            now = time.time()
            include_pss = now >= next_pss_at
            if include_pss:
                next_pss_at = now + args.pss_interval
            if now >= next_discovery_at:
                known_pids = discover_descendants(known_pids)
                next_discovery_at = now + args.discovery_interval
            # Fast path: read full metadata only for roots and previously
            # discovered descendants, rather than for every host process.
            processes = {pid: info for pid in known_pids if (info := read_process(pid)) is not None}
            # PSS is much more expensive than process discovery.  Restrict the
            # mapping walk to selected roots/descendants, never every process
            # visible on the host, so CPU/core sampling remains near its
            # requested cadence.
            if include_pss:
                for pid in known_pids:
                    info = processes.get(pid)
                    if info:
                        info["pss_bytes"] = read_pss(pid)
            current_keys = set()
            for pid in known_pids:
                info = processes.get(pid)
                if not info:
                    continue
                key = (pid, info["start_ticks"])
                current_keys.add(key)
                if key not in seen:
                    seen.add(key)
                    active[key] = info
                    stream.write(json.dumps({"kind": "process", "event": "start", "observed_at": now, **info}, separators=(",", ":")) + "\n")
                stream.write(
                    json.dumps(
                        {
                            "kind": "process",
                            "event": "sample",
                            "observed_at": now,
                            "pid": pid,
                            "start_ticks": info["start_ticks"],
                            "cpu_ms": round((info["utime_ticks"] + info["stime_ticks"]) * 1000 / tick_hz, 3),
                            "cpu_number": info["cpu_number"],
                            "pss_bytes": info["pss_bytes"],
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            for key, info in list(active.items()):
                if key not in current_keys:
                    stream.write(json.dumps({"kind": "process", "event": "exit_observed", "observed_at": now, "pid": info["pid"], "start_ticks": info["start_ticks"]}, separators=(",", ":")) + "\n")
                    del active[key]
            stream.flush()
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
