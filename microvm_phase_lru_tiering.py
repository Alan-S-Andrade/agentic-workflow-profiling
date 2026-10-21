#!/usr/bin/env python3
"""Whole-VM NUMA tiering, demoting sessions by their current wait phase.

Alongside the existing launch-time LRU controller, this version reads one
atomic JSON state file per session from --phase-dir. Files are named
session-001.json and contain {"phase": ..., "updated_at": <epoch seconds>}.
Recognized phases, from most to least demotable, are waiting_user_input,
waiting_llm_inference, and running_tool. The state publisher must refresh
updated_at while a phase remains active; missing or stale state is not eligible
for demotion. Ties within a phase use launch-time LRU. Promotion keeps the
original policy: most recently launched slow-tier VM first.

The remote trace runner publishes this state under each stage's phases/
directory. Use --dry-run to inspect selected VMs without moving pages.
"""

import argparse
import json
import os
import signal
import time
from pathlib import Path

from microvm_lru_tiering import (
    GIB,
    Migrator,
    VM,
    discover,
    emit,
    node_free,
    page_tpp_enabled,
    residency,
    set_process_affinity,
)


PHASE_PRIORITY = {
    "waiting_user_input": 0,
    "waiting_llm_inference": 1,
    "running_tool": 2,
}


def read_phase(phase_dir, session, now, max_age):
    """Return a current recognized phase, or None when state is unusable."""
    path = phase_dir / f"{session}.json"
    try:
        state = json.loads(path.read_text())
        phase = state["phase"]
        updated_at = float(state["updated_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if phase not in PHASE_PRIORITY or now - updated_at > max_age or updated_at > now + 5:
        return None
    return phase


def choose_move(vms, phase_dir, fast, slow, fast_free, slow_free,
                demote_below, promote_above, slow_reserve, cooldown,
                max_phase_age, now):
    """Select one whole-VM move; phase priority applies only to demotion."""
    eligible = [vm for vm in vms if now - vm.last_move >= cooldown]
    if fast_free < demote_below:
        candidates = []
        for vm in eligible:
            if vm.tier != fast:
                continue
            phase = read_phase(phase_dir, vm.session, time.time(), max_phase_age)
            if phase is None:
                continue
            try:
                counts = residency(vm.pid)
            except OSError:
                continue
            if counts.get(fast, 0) and slow_free >= counts[fast] + slow_reserve:
                candidates.append((PHASE_PRIORITY[phase], vm.launched_at,
                                   vm.session, vm, phase, counts))
        if candidates:
            _, _, _, vm, phase, counts = min(candidates)
            return vm, fast, slow, counts, phase
    elif fast_free > promote_above:
        for vm in sorted((vm for vm in eligible if vm.tier == slow),
                         key=lambda vm: (vm.launched_at, vm.session), reverse=True):
            try:
                counts = residency(vm.pid)
            except OSError:
                continue
            if counts.get(slow, 0) and fast_free >= counts[slow] + promote_above:
                return vm, slow, fast, counts, None
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-root", required=True, type=Path)
    parser.add_argument("--phase-dir", required=True, type=Path,
                        help="directory of per-session phase JSON files")
    parser.add_argument("--fast-node", type=int, default=0)
    parser.add_argument("--slow-node", type=int, default=1)
    parser.add_argument("--demote-free-gib", type=float, default=10)
    parser.add_argument("--promote-free-gib", type=float, default=12)
    parser.add_argument("--slow-reserve-gib", type=float, default=2)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--cooldown-seconds", type=float, default=5)
    parser.add_argument("--max-phase-age-seconds", type=float, default=30,
                        help="older phase files are ignored; publisher should heartbeat")
    parser.add_argument("--duration", type=float, default=0,
                        help="seconds; 0 means until interrupted")
    parser.add_argument("--move-cpus-with-memory", action="store_true",
                        help="move current VMM threads to the node holding the VM")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 and not args.dry_run:
        parser.error("live migration requires root")
    if args.fast_node == args.slow_node or min(args.fast_node, args.slow_node) < 0:
        parser.error("choose distinct nonnegative NUMA nodes")
    if (not 0 <= args.demote_free_gib < args.promote_free_gib or
            min(args.slow_reserve_gib, args.cooldown_seconds,
                args.max_phase_age_seconds, args.duration) < 0 or args.interval <= 0):
        parser.error("invalid thresholds, interval, or duration")
    if not args.dry_run and page_tpp_enabled():
        parser.error("disable kernel NUMA balancing and demotion before this whole-VM experiment")
    node_free(args.fast_node)
    node_free(args.slow_node)
    mover = None if args.dry_run else Migrator(args.fast_node, args.slow_node)
    known = {}
    deadline = time.monotonic() + args.duration if args.duration else float("inf")
    signal.signal(signal.SIGINT, signal.default_int_handler)
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
            decision = choose_move(
                known.values(), args.phase_dir, args.fast_node, args.slow_node,
                node_free(args.fast_node), node_free(args.slow_node),
                args.demote_free_gib * GIB, args.promote_free_gib * GIB,
                args.slow_reserve_gib * GIB, args.cooldown_seconds,
                args.max_phase_age_seconds, time.time())
            if decision:
                vm, source, target, before, phase = decision
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
                        emit("planned" if args.dry_run else "migrated",
                             session=vm.session, pid=vm.pid,
                             launched_at=vm.launched_at, source=source, target=target,
                             phase=phase, seconds=time.monotonic() - begun,
                             unmoved_pages=unmoved, before_bytes=before, after_bytes=after,
                             cpu_node=target if args.move_cpus_with_memory else None,
                             cpus=affinity[0] if affinity else None,
                             affinity_threads_changed=affinity[1] if affinity else None)
                    except OSError as exc:
                        vm.last_move = time.monotonic()
                        emit("migration_failed", session=vm.session, pid=vm.pid,
                             source=source, target=target, phase=phase, error=str(exc))
            time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        pass
    finally:
        if mover:
            mover.close()
        emit("stopped")


if __name__ == "__main__":
    main()
