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


def read_process(pid: int) -> dict | None:
    proc = Path("/proc") / str(pid)
    try:
        raw_stat = (proc / "stat").read_text()
        close = raw_stat.rfind(")")
        fields = raw_stat[close + 2 :].split()
        status = (proc / "status").read_text()
        rss_kib = next((int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS:")), None)
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
        "rss_bytes": rss_kib * 1024 if rss_kib is not None else None,
        "cmdline": cmdline,
        "cgroup": cgroup,
        "namespaces": ns,
    }


def snapshot() -> dict[int, dict]:
    result = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            info = read_process(int(entry.name))
            if info:
                result[info["pid"]] = info
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-pid", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.1)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    tick_hz = os.sysconf("SC_CLK_TCK")
    known_pids = {args.root_pid}
    seen: set[tuple[int, int]] = set()
    active: dict[tuple[int, int], dict] = {}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("a", encoding="utf-8") as stream:
        while running:
            now = time.time()
            processes = snapshot()
            # Resolve descendants repeatedly so a grandchild discovered in the
            # same scan is retained even when its parent was just discovered.
            changed = True
            while changed:
                changed = False
                for info in processes.values():
                    if info["ppid"] in known_pids and info["pid"] not in known_pids:
                        known_pids.add(info["pid"])
                        changed = True
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
                            "rss_bytes": info["rss_bytes"],
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
