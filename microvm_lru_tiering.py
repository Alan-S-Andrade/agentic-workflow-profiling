#!/usr/bin/env python3
"""Move complete SWE-bench microVM processes between NUMA tiers by session age.

Run alongside swebench_microvm_scale_experiment.py with its compressed Azure
--admission-schedule. Every new session starts on node 0. When node 0 runs low,
demote the VM whose *session was launched earliest*. When it has room again,
promote the most recently launched VM on node 1. Inference turns do not update
this order. Each move attempts every resident page in the VMM process; the log
reports pages that could not be moved and actual before/after node residency.
"""

import argparse
import ctypes
import ctypes.util
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

GIB = 1024 ** 3


def emit(event, **fields):
    print(json.dumps({"timestamp": time.time(), "event": event, **fields}), flush=True)


def node_free(node):
    data = Path(f"/sys/devices/system/node/node{node}/meminfo").read_text()
    return int(re.search(r"MemFree:\s+(\d+)", data)[1]) * 1024


def start_ticks(pid):
    data = Path(f"/proc/{pid}/stat").read_text()
    return int(data[data.rfind(")") + 2:].split()[19])


def node_cpus(node):
    raw = Path(f"/sys/devices/system/node/node{node}/cpulist").read_text().strip()
    cpus = set()
    for part in raw.split(","):
        ends = [int(value) for value in part.split("-")]
        cpus.update(range(ends[0], ends[-1] + 1))
    return cpus


def set_process_affinity(pid, node):
    """Set affinity for every current VMM thread to CPUs on one NUMA node."""
    cpus = node_cpus(node)
    tasks = Path(f"/proc/{pid}/task")
    changed = 0
    errors = []
    for entry in tasks.iterdir():
        tid = int(entry.name)
        try:
            if os.sched_getaffinity(tid) != cpus:
                os.sched_setaffinity(tid, cpus)
                changed += 1
        except ProcessLookupError:
            continue
        except OSError as exc:
            errors.append(f"tid {tid}: {exc}")
    if errors:
        raise OSError("; ".join(errors))
    return sorted(cpus), changed


def residency(pid):
    """Resident VMM bytes by host NUMA node, including guest RAM."""
    result = {}
    for line in Path(f"/proc/{pid}/numa_maps").read_text().splitlines():
        match = re.search(r"kernelpagesize_kB=(\d+)", line)
        size = int(match[1]) * 1024 if match else os.sysconf("SC_PAGE_SIZE")
        for node, pages in re.findall(r"\bN(\d+)=(\d+)", line):
            result[int(node)] = result.get(int(node), 0) + int(pages) * size
    return result


@dataclass
class VM:
    path: Path
    pid: int
    start: int
    session: str
    launched_at: float
    tier: int
    last_move: float = 0.0

    def alive(self):
        try:
            return start_ticks(self.pid) == self.start
        except (OSError, ValueError, IndexError):
            return False


def discover(root, known, fast, slow, move_cpus):
    for path in root.glob("sessions-*/vms/session-*/sandbox.json"):
        if path in known:
            continue
        try:
            meta = json.loads(path.read_text())
            pid = int(meta["pid"])
            counts = residency(pid)
            tier = slow if counts.get(slow, 0) > counts.get(fast, 0) else fast
            vm = VM(path, pid, int(meta["start_ticks"]), path.parent.name,
                    float(meta["launched_at"]), tier)
            if vm.alive():
                affinity = set_process_affinity(pid, tier) if move_cpus else None
                known[path] = vm
                emit("discovered", session=vm.session, pid=pid, launched_at=vm.launched_at,
                     tier=tier, node_bytes=counts,
                     cpu_node=tier if move_cpus else None,
                     cpus=affinity[0] if affinity else None)
        except (OSError, ValueError, KeyError, IndexError) as exc:
            # A VM may still be starting or may have just exited.
            emit("discovery_failed", sandbox=str(path.parent), error=str(exc))
            continue


def choose_move(vms, fast, slow, fast_free, slow_free, demote_below,
                promote_above, slow_reserve, cooldown, now):
    """Return (VM, source, target, residency) for one whole-VM move."""
    eligible = [vm for vm in vms if now - vm.last_move >= cooldown]
    if fast_free < demote_below:
        for vm in sorted((vm for vm in eligible if vm.tier == fast),
                         key=lambda vm: (vm.launched_at, vm.session)):
            try:
                counts = residency(vm.pid)
            except OSError:
                continue
            if counts.get(fast, 0) and slow_free >= counts[fast] + slow_reserve:
                return vm, fast, slow, counts
    elif fast_free > promote_above:
        for vm in sorted((vm for vm in eligible if vm.tier == slow),
                         key=lambda vm: (vm.launched_at, vm.session), reverse=True):
            try:
                counts = residency(vm.pid)
            except OSError:
                continue
            if counts.get(slow, 0) and fast_free >= counts[slow] + promote_above:
                return vm, slow, fast, counts
    return None


class Migrator:
    def __init__(self, fast, slow):
        self.lib = ctypes.CDLL(ctypes.util.find_library("numa") or "libnuma.so.1",
                               use_errno=True)
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
        unmoved = self.lib.numa_migrate_pages(pid, self.masks[source], self.masks[target])
        if unmoved < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return unmoved

    def close(self):
        for mask in self.masks.values():
            self.lib.numa_bitmask_free(mask)


def page_tpp_enabled():
    balancing = Path("/proc/sys/kernel/numa_balancing")
    demotion = Path("/sys/kernel/mm/numa/demotion_enabled")
    return (balancing.exists() and balancing.read_text().strip() != "0" or
            demotion.exists() and demotion.read_text().strip().lower() in ("1", "true"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-root", required=True, type=Path)
    parser.add_argument("--fast-node", type=int, default=0)
    parser.add_argument("--slow-node", type=int, default=1)
    parser.add_argument("--demote-free-gib", type=float, default=10)
    parser.add_argument("--promote-free-gib", type=float, default=12)
    parser.add_argument("--slow-reserve-gib", type=float, default=2)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--cooldown-seconds", type=float, default=5)
    parser.add_argument("--duration", type=float, default=0, help="seconds; 0 means until interrupted")
    parser.add_argument("--move-cpus-with-memory", action="store_true",
                        help="move every VMM thread to the NUMA node holding its VM memory")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 and not args.dry_run:
        parser.error("live migration requires root")
    if args.fast_node == args.slow_node or min(args.fast_node, args.slow_node) < 0:
        parser.error("choose distinct nonnegative NUMA nodes")
    if (not 0 <= args.demote_free_gib < args.promote_free_gib or
            min(args.slow_reserve_gib, args.cooldown_seconds, args.duration) < 0 or
            args.interval <= 0):
        parser.error("invalid thresholds, interval, or duration")
    if not args.dry_run and page_tpp_enabled():
        parser.error("disable kernel NUMA balancing and demotion before this whole-VM experiment")
    node_free(args.fast_node)
    node_free(args.slow_node)
    mover = None if args.dry_run else Migrator(args.fast_node, args.slow_node)
    known = {}
    deadline = time.monotonic() + args.duration if args.duration else float("inf")
    emit("started", **{key: str(value) if isinstance(value, Path) else value
                       for key, value in vars(args).items()})
    try:
        while time.monotonic() < deadline:
            discover(args.trace_root, known, args.fast_node, args.slow_node,
                     args.move_cpus_with_memory and not args.dry_run)
            for path, vm in list(known.items()):
                if not vm.alive():
                    emit("exited", session=vm.session, pid=vm.pid, tier=vm.tier)
                    del known[path]
            decision = choose_move(known.values(), args.fast_node, args.slow_node,
                                   node_free(args.fast_node), node_free(args.slow_node),
                                   args.demote_free_gib * GIB, args.promote_free_gib * GIB,
                                   args.slow_reserve_gib * GIB, args.cooldown_seconds,
                                   time.monotonic())
            if decision:
                vm, source, target, before = decision
                if vm.alive():
                    try:
                        begun = time.monotonic()
                        unmoved = None if args.dry_run else mover.move(vm.pid, source, target)
                        after = before if args.dry_run else residency(vm.pid)
                        vm.last_move = time.monotonic()
                        if not args.dry_run:
                            vm.tier = target
                        affinity = None
                        if args.move_cpus_with_memory and not args.dry_run:
                            try:
                                affinity = set_process_affinity(vm.pid, target)
                            except OSError as exc:
                                emit("affinity_failed", session=vm.session, pid=vm.pid,
                                     cpu_node=target, error=str(exc))
                        emit("planned" if args.dry_run else "migrated", session=vm.session,
                             pid=vm.pid, launched_at=vm.launched_at, source=source,
                             target=target, seconds=time.monotonic() - begun,
                             unmoved_pages=unmoved, before_bytes=before, after_bytes=after,
                             cpu_node=target if args.move_cpus_with_memory else None,
                             cpus=affinity[0] if affinity else None,
                             affinity_threads_changed=affinity[1] if affinity else None)
                    except OSError as exc:
                        vm.last_move = time.monotonic()
                        emit("migration_failed", session=vm.session, pid=vm.pid,
                             source=source, target=target, error=str(exc))
            time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        pass
    finally:
        if mover:
            mover.close()
        emit("stopped")


if __name__ == "__main__":
    main()
