#!/usr/bin/env python3
"""Tier whole capacity-experiment microVMs by their last inference/prompt request.

Run as root on node 1 alongside swebench_microvm_scale_experiment.py. New VMs
publish sandbox.json and REPLAY_REQUEST records; startup VMs are never cold.
Cold sandboxes are demoted under node-0 pressure, oldest first. A new request
makes a sandbox hot; promote its resident memory when node 0 has headroom.
CPU placement stays on node 0. No page-access sampling is performed.
"""

import argparse
import base64
import ctypes
import ctypes.util
import json
import os
import re
import time
from pathlib import Path

GIB = 1024 ** 3


def emit(event, **fields):
    print(json.dumps(dict(timestamp=time.time(), event=event, **fields)), flush=True)


def node_free(node):
    text = Path(f"/sys/devices/system/node/node{node}/meminfo").read_text()
    return int(re.search(r"MemFree:\s+(\d+)", text)[1]) * 1024


def start_ticks(pid):
    stat = Path(f"/proc/{pid}/stat").read_text()
    return int(stat[stat.rfind(")") + 2:].split()[19])


def residency(pid):
    """Count resident bytes per node for the entire VMM, including guest RAM."""
    result = {}
    for line in Path(f"/proc/{pid}/numa_maps").read_text().splitlines():
        size = re.search(r"kernelpagesize_kB=(\d+)", line)
        page_size = int(size[1]) * 1024 if size else os.sysconf("SC_PAGE_SIZE")
        for node, count in re.findall(r"\bN(\d+)=(\d+)", line):
            result[int(node)] = result.get(int(node), 0) + int(count) * page_size
    return result


class Migrator:
    """migrate_pages acts on all resident pages of a process, not selected pages."""

    def __init__(self, fast, slow):
        self.lib = ctypes.CDLL(ctypes.util.find_library("numa") or "libnuma.so.1", use_errno=True)
        self.lib.numa_allocate_nodemask.restype = ctypes.c_void_p
        self.lib.numa_bitmask_setbit.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.lib.numa_bitmask_setbit.restype = ctypes.c_void_p
        self.lib.numa_bitmask_free.argtypes = [ctypes.c_void_p]
        self.lib.numa_migrate_pages.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        self.lib.numa_migrate_pages.restype = ctypes.c_int
        self.masks = {}
        for node in (fast, slow):
            mask = self.lib.numa_allocate_nodemask()
            if not mask:
                raise MemoryError("cannot allocate NUMA nodemask")
            self.lib.numa_bitmask_setbit(mask, node)
            self.masks[node] = mask

    def move(self, pid, source, target):
        result = self.lib.numa_migrate_pages(pid, self.masks[source], self.masks[target])
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return result  # Nonzero means some pages could not be moved.

    def close(self):
        for mask in self.masks.values():
            self.lib.numa_bitmask_free(mask)


class Sandbox:
    def __init__(self, path, metadata):
        self.path, self.meta = path, metadata
        self.pid = int(metadata["pid"])
        self.offset, self.pending = 0, b""
        self.last_request = None
        self.last_move = 0.0
        self.last_target = None

    def alive(self):
        try:
            return start_ticks(self.pid) == self.meta["start_ticks"]
        except (OSError, ValueError, IndexError):
            return False

    def activity(self):
        """Tail request events incrementally; guest timestamps identify actual requests."""
        try:
            with open(self.meta["serial_path"], "rb") as stream:
                stream.seek(self.offset)
                data = stream.read()
                self.offset = stream.tell()
        except FileNotFoundError:
            return
        lines = (self.pending + data).split(b"\n")
        self.pending = lines.pop()
        for line in lines:
            match = re.search(rb"REPLAY_REQUEST ([A-Za-z0-9+/=]+)", line)
            if match:
                try:
                    stamp = float(json.loads(base64.b64decode(match[1]))["timestamp"])
                    self.touch(stamp)
                except (ValueError, KeyError):
                    continue

    def touch(self, stamp):
        stamp = min(stamp, time.time())
        self.last_request = max(self.last_request or stamp, stamp)


def discover(root, tracked):
    for path in root.glob("sessions-*/vms/session-*/sandbox.json"):
        if path in tracked:
            continue
        try:
            sandbox = Sandbox(path, json.loads(path.read_text()))
            if sandbox.alive():
                tracked[path] = sandbox
                emit("discovered", pid=sandbox.pid, sandbox=str(path.parent))
        except (OSError, ValueError, KeyError):
            continue


def move_sandbox(sandbox, mover, source, target, args):
    # Identity is checked again immediately before touching a process.
    if not sandbox.alive():
        return
    if sandbox.last_target == target and time.monotonic() - sandbox.last_move < args.cooldown_seconds:
        return
    before = residency(sandbox.pid)
    try:
        began = time.monotonic()
        unmoved = None if args.dry_run else mover.move(sandbox.pid, source, target)
        after = before if args.dry_run else residency(sandbox.pid)
        sandbox.last_move = time.monotonic()
        sandbox.last_target = target
        emit("planned" if args.dry_run else "migrated", pid=sandbox.pid,
             source=source, target=target, idle_seconds=time.time() - sandbox.last_request,
             seconds=time.monotonic() - began, unmoved_pages=unmoved,
             before_bytes=before, after_bytes=after)
    except OSError as exc:
        sandbox.last_move = time.monotonic()
        sandbox.last_target = target
        emit("migration_failed", pid=sandbox.pid, source=source, target=target, error=str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-root", required=True, type=Path)
    parser.add_argument("--fast-node", type=int, default=0)
    parser.add_argument("--slow-node", type=int, default=1)
    parser.add_argument("--idle-seconds", type=float, default=30)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--demote-free-gib", type=float, default=10)
    parser.add_argument("--promote-free-gib", type=float, default=12)
    parser.add_argument("--slow-reserve-gib", type=float, default=2)
    parser.add_argument("--cooldown-seconds", type=float, default=5)
    parser.add_argument("--offload-all-cold", action="store_true",
                        help="demote idle sandboxes even without fast-node memory pressure")
    parser.add_argument("--prompt-log", type=Path,
                        help='optional host JSONL prompt events: {"pid": VMM_PID, "timestamp": UNIX_SECONDS}')
    parser.add_argument("--duration", type=float, default=0, help="0 runs until interrupted")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run as root to inspect and migrate VMM memory")
    if args.fast_node == args.slow_node:
        parser.error("fast and slow nodes must differ")
    if min(args.idle_seconds, args.interval) <= 0 or min(args.cooldown_seconds, args.duration) < 0:
        parser.error("interval and idle timeout must be positive; cooldown/duration nonnegative")
    if not 0 <= args.demote_free_gib < args.promote_free_gib or args.slow_reserve_gib < 0:
        parser.error("require 0 <= demotion watermark < promotion watermark and nonnegative reserve")
    for node in (args.fast_node, args.slow_node):
        node_free(node)  # Validate both NUMA nodes before monitoring.
    tracked, prompt_offset, prompt_pending = {}, 0, b""
    mover = Migrator(args.fast_node, args.slow_node)
    deadline = time.monotonic() + args.duration if args.duration else float("inf")
    emit("started", **{k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()})
    try:
        while time.monotonic() < deadline:
            discover(args.trace_root, tracked)
            active = []
            for sandbox in list(tracked.values()):
                if not sandbox.alive():
                    del tracked[sandbox.path]
                    continue
                sandbox.activity()
                active.append(sandbox)
            if args.prompt_log and args.prompt_log.exists():
                with args.prompt_log.open("rb") as stream:
                    stream.seek(prompt_offset)
                    lines = (prompt_pending + stream.read()).split(b"\n")
                    prompt_offset, prompt_pending = stream.tell(), lines.pop()
                for line in lines:
                    try:
                        event = json.loads(line)
                        for sandbox in active:
                            if sandbox.pid == event["pid"]:
                                sandbox.touch(float(event["timestamp"]))
                    except (ValueError, KeyError):
                        continue
            eligible = [s for s in active if s.last_request is not None]
            # Promote recent requests first; demote the oldest idle sandbox first.
            for sandbox in sorted(eligible, key=lambda s: s.last_request, reverse=True):
                if time.time() - sandbox.last_request >= args.idle_seconds:
                    continue
                try:
                    slow_bytes = residency(sandbox.pid).get(args.slow_node, 0)
                    if slow_bytes and node_free(args.fast_node) >= slow_bytes + args.promote_free_gib * GIB:
                        move_sandbox(sandbox, mover, args.slow_node, args.fast_node, args)
                except OSError:
                    continue
            for sandbox in sorted(eligible, key=lambda s: s.last_request):
                sandbox.activity()  # Recheck for a request arriving during earlier migrations.
                if time.time() - sandbox.last_request < args.idle_seconds:
                    continue
                if not args.offload_all_cold and node_free(args.fast_node) >= args.demote_free_gib * GIB:
                    break
                try:
                    fast_bytes = residency(sandbox.pid).get(args.fast_node, 0)
                    if fast_bytes and node_free(args.slow_node) >= fast_bytes + args.slow_reserve_gib * GIB:
                        move_sandbox(sandbox, mover, args.fast_node, args.slow_node, args)
                except OSError:
                    continue
            time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        pass
    finally:
        mover.close()
        emit("stopped")


if __name__ == "__main__":
    main()
