#!/usr/bin/env python3
"""Collect per-process hardware counters for one OpenHands human turn.

The profiler starts before the user event is submitted and is stopped after
the corresponding FinishAction.  It follows the supplied root process tree and
starts one ``perf stat`` process for every discovered descendant.  This makes
the attribution explicit, but short-lived processes can still escape the 10 ms
discovery interval; those are reported as not observed, never as zero.

Only counters advertised by this host are enabled.  In particular, generic
``cache-misses`` is *not* an LLC-miss measurement, and memory bandwidth is
unavailable unless a memory-controller/DRAM PMU event is exposed by perf.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time


RUNNING = True
GENERIC_EVENTS = ("cycles", "instructions", "cache-misses")
LLC_CANDIDATES = ("LLC-loads", "LLC-load-misses", "llc-loads", "llc-load-misses")
TLB_CANDIDATES = ("dTLB-load-misses", "iTLB-load-misses", "l1d_tlb_refill", "l1i_tlb_refill")
L2_CANDIDATES = ("l2d_cache_refill",)
MEMORY_BW_CANDIDATES = (
    "uncore_imc_0/cas_count_read/", "uncore_imc_0/cas_count_write/",
    "arm_dsu_0/bus_access/",
)


def stop(*_args):
    global RUNNING
    RUNNING = False


def proc_info(pid: int):
    proc = Path("/proc") / str(pid)
    try:
        stat = (proc / "stat").read_text()
        end = stat.rfind(")")
        fields = stat[end + 2:].split()
        return {
            "pid": pid,
            "ppid": int(fields[1]),
            "start_ticks": int(fields[19]),
            "cmdline": (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip(),
        }
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None


def all_processes():
    return {pid: info for pid in (int(x.name) for x in Path("/proc").iterdir() if x.name.isdigit()) if (info := proc_info(pid))}


def supported(events):
    # ``perf list`` is a capability listing; it does not prove a counter can
    # actually be scheduled, so perf's per-PID output remains authoritative.
    listing = subprocess.run(["perf", "list", "--no-desc"], text=True, capture_output=True, check=False).stdout.lower()
    return [event for event in events if event.lower() in listing]


def parse_perf(stderr: str):
    values = {}
    for line in stderr.splitlines():
        columns = line.split(",")
        if len(columns) < 3:
            continue
        raw, event = columns[0].strip(), columns[2].strip()
        if not event:
            continue
        try:
            values[event] = int(raw.replace(" ", ""))
        except ValueError:
            values[event] = None
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-pid", type=int, required=True)
    parser.add_argument("--turn-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.05)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    generic = supported(GENERIC_EVENTS)
    llc = supported(LLC_CANDIDATES)
    tlb = supported(TLB_CANDIDATES)
    l2 = supported(L2_CANDIDATES)
    bandwidth = supported(MEMORY_BW_CANDIDATES)
    events = generic + llc + tlb + l2 + bandwidth
    profilers, known = {}, {args.root_pid}
    started = time.time()
    while RUNNING:
        processes = all_processes()
        changed = True
        while changed:
            changed = False
            for info in processes.values():
                if info["ppid"] in known and info["pid"] not in known:
                    known.add(info["pid"])
                    changed = True
        for pid in known:
            info = processes.get(pid)
            key = (pid, info["start_ticks"]) if info else None
            if not info or key in profilers:
                continue
            command = ["perf", "stat", "-x,", "-p", str(pid), "-e", ",".join(events)]
            profilers[key] = (info, subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True))
        time.sleep(args.interval)
    results = []
    for (_pid, _start), (info, process) in profilers.items():
        if process.poll() is None:
            # perf flushes its counter report on SIGINT; SIGTERM can discard it.
            process.send_signal(signal.SIGINT)
        try:
            _out, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            _out, stderr = process.communicate()
        results.append({**info, "counters": parse_perf(stderr), "perf_stderr": stderr.strip()})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "turn_id": args.turn_id,
        "started_at": started,
        "ended_at": time.time(),
        "discovery_interval_ms": args.interval * 1000,
        "generic_events": generic,
        "llc_events": llc,
        "tlb_events": tlb,
        "l2_events": l2,
        "memory_bandwidth_events": bandwidth,
        "limitations": {
            "llc": "unavailable" if not llc else None,
            "memory_bandwidth": "unavailable" if not bandwidth else None,
            "short_lived_processes": "may be missed between discovery scans",
        },
        "processes": results,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
