#!/usr/bin/env python3
"""Autonomous `/yig` SWE-bench replay at fixed concurrency points.

Runs 1, 2, 4, 8, 16, 32, 64, and 128 simultaneous Cloud Hypervisor sessions.
The host never controls a guest after launch.  Each guest sleeps for recorded
model time, executes its recorded commands locally, and powers itself off.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_STAGES = (1, 2, 4, 8, 16, 32, 64, 128)


class MemoryGuard(Exception):
    """Admission stopped before the host reached the configured OOM guard."""
GUEST_REPLAY = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, tempfile, time
from pathlib import Path
MAX_CAPTURE = 65536
def command(cmd):
    begun = time.time()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        result = subprocess.run(["chroot", "/rw", "/bin/bash", "-lc", "cd /testbed && " + cmd], stdin=subprocess.DEVNULL, stdout=out, stderr=err, check=False)
        osize, esize = out.tell(), err.tell(); out.seek(0); err.seek(0)
        return {"command": cmd, "started_at": begun, "finished_at": time.time(), "elapsed_seconds": time.time() - begun, "returncode": result.returncode, "stdout": out.read(MAX_CAPTURE).decode("utf-8", "replace"), "stderr": err.read(MAX_CAPTURE).decode("utf-8", "replace"), "stdout_truncated": osize > MAX_CAPTURE, "stderr_truncated": esize > MAX_CAPTURE}
def main():
    manifest_path = Path('/rw/replay/manifest.json'); manifest = json.loads(manifest_path.read_text()); replay = manifest_path.parent
    tool_records = (replay / 'guest-tools.jsonl').open('w'); turn_records = (replay / 'guest-turns.jsonl').open('w')
    failures = tools = 0
    for turn in manifest['turns']:
        started = time.time(); time.sleep(max(0.0, float(turn['inference_seconds'])))
        turn_failures = 0
        for action, cmd in enumerate(turn['commands'], 1):
            record = command(cmd); record.update({'turn': turn['turn'], 'action': action}); tool_records.write(json.dumps(record) + '\n'); tool_records.flush()
            tools += 1; turn_failures += record['returncode'] != 0
        failures += turn_failures
        turn_records.write(json.dumps({'turn': turn['turn'], 'started_at': started, 'finished_at': time.time(), 'recorded_inference_seconds': turn['inference_seconds'], 'tool_commands': len(turn['commands']), 'tool_failures': turn_failures}) + '\n'); turn_records.flush()
    tool_records.close(); turn_records.close()
    (replay / 'summary.json').write_text(json.dumps({'instance_id': manifest['instance_id'], 'source_trajectory': manifest['source_trajectory'], 'tools': tools, 'tool_failures': failures, 'turns': len(manifest['turns'])}, indent=2) + '\n'); os.sync()
if __name__ == '__main__': main()
'''
GUEST_INIT = r'''#!/bin/bash
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /run /tmp /rw
mount -t ext4 /dev/vdb /rw
mount -t tmpfs -o nosuid,nodev tmpfs /tmp
/usr/bin/python3 /usr/local/bin/swebench-local-replay.py
status=$?
sync
/bin/busybox poweroff -f
exit "$status"
'''


def call(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(list(map(str, args)), check=True, text=True, **kwargs)


def sudo(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return call("sudo", "-n", *args, **kwargs)


def parse_cpu_list(raw: str) -> list[int]:
    result: list[int] = []
    for part in raw.split(","):
        if "-" in part:
            first, last = map(int, part.split("-", 1)); result.extend(range(first, last + 1))
        else:
            result.append(int(part))
    return sorted(set(result))


def cpuset() -> list[int]:
    """Return all CPUs available to this process unless explicitly overridden."""
    override = os.environ.get("MICROVM_CPUSET")
    if override:
        return parse_cpu_list(override)
    online = Path("/sys/devices/system/cpu/online")
    cpus = parse_cpu_list(online.read_text().strip()) if online.exists() else []
    affinity = sorted(os.sched_getaffinity(0))
    return sorted(set(cpus).intersection(affinity)) if cpus else affinity


def format_cpu_list(cpus: list[int]) -> str:
    if not cpus:
        raise RuntimeError("no CPUs are available for the microVM")
    ranges: list[str] = []
    start = previous = cpus[0]
    for cpu in cpus[1:]:
        if cpu == previous + 1:
            previous = cpu
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = cpu
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def numa_nodes() -> list[Path]:
    nodes = sorted(Path("/sys/devices/system/node").glob("node[0-9]*"),
                   key=lambda p: int(p.name[4:]))
    if not nodes:
        raise RuntimeError("could not determine host NUMA nodes")
    return nodes


def _node_meminfo(node: Path) -> tuple[int, int]:
    text = (node / "meminfo").read_text()
    prefix = re.escape(node.name.replace("node", "Node "))
    total = re.search(rf"^{prefix} MemTotal:\s+(\d+) kB", text, re.M)
    available = re.search(rf"^{prefix} MemAvailable:\s+(\d+) kB", text, re.M)
    used = re.search(rf"^{prefix} MemUsed:\s+(\d+) kB", text, re.M)
    free = re.search(rf"^{prefix} MemFree:\s+(\d+) kB", text, re.M)
    if not total:
        raise RuntimeError(f"could not determine memory for {node.name}")
    total_bytes = int(total.group(1)) * 1024
    if available:
        used_bytes = total_bytes - int(available.group(1)) * 1024
    elif used:
        used_bytes = int(used.group(1)) * 1024
    elif free:
        used_bytes = total_bytes - int(free.group(1)) * 1024
    else:
        raise RuntimeError(f"could not determine used memory for {node.name}")
    return total_bytes, used_bytes


def host_memory_bytes() -> int:
    return sum(_node_meminfo(node)[0] for node in numa_nodes())


def host_used_bytes() -> int:
    return sum(_node_meminfo(node)[1] for node in numa_nodes())


def swap_bytes() -> int:
    """Return configured swap capacity in bytes."""
    total = 0
    for line in Path("/proc/swaps").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3:
            total += int(fields[2]) * 1024
    return total


def filesystem_type(path: Path) -> str:
    return subprocess.check_output(["findmnt", "-T", str(path), "-no", "FSTYPE"], text=True).strip()


def cgroup_value(path: Path, field: str) -> int:
    try:
        return int((path / field).read_text().strip())
    except (FileNotFoundError, PermissionError, ValueError):
        return 0


def cpu_usage_usec(path: Path) -> int:
    try:
        match = re.search(r"^usage_usec\s+(\d+)$", (path / "cpu.stat").read_text(), re.M)
        return int(match.group(1)) if match else 0
    except (FileNotFoundError, PermissionError):
        return 0


def pss_bytes(path: Path) -> int:
    try:
        pids = [int(x) for x in (path / "cgroup.procs").read_text().split()]
    except (FileNotFoundError, PermissionError):
        return 0
    total = 0
    for pid in pids:
        try:
            match = re.search(r"^Pss:\s+(\d+) kB", Path(f"/proc/{pid}/smaps_rollup").read_text(), re.M)
            total += int(match.group(1)) * 1024 if match else 0
        except (FileNotFoundError, PermissionError):
            pass
    return total


def image_name(instance_id: str) -> str:
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_').lower()}:latest"


def manifest_for_trace(trace: Path, repetition: int) -> dict:
    trace = trace.resolve()
    data = json.loads(trace.read_text())
    tool_turns = [m for m in data["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]
    turns = []
    for index, timing in enumerate(data.get("step_timings", [])):
        commands = []
        calls = tool_turns[index].get("tool_calls", []) if index < len(tool_turns) else []
        for call_data in calls:
            function = call_data.get("function", {})
            if function.get("name") != "bash":
                continue
            try:
                command = json.loads(function["arguments"])["command"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid bash call in {trace}") from exc
            if isinstance(command, str):
                commands.append(command)
        turns.append({"turn": timing.get("step", index + 1),
                      "inference_seconds": float(timing.get("inference", {}).get("elapsed_s", 0.0)),
                      "commands": commands})
    return {"instance_id": data["instance_id"], "source_trajectory": str(trace.relative_to(ROOT)),
            "repetition": repetition, "turns": turns}


def load_traces(trace_root: Path) -> list[tuple[Path, dict]]:
    trace_root = trace_root.resolve()
    selected, ids = [], set()
    for trace in sorted(trace_root.glob("*/results/*/*.traj.json")):
        manifest = manifest_for_trace(trace, 0)
        if manifest["instance_id"] not in ids:
            selected.append((trace, manifest)); ids.add(manifest["instance_id"])
    if len(selected) < 100:
        raise SystemExit(f"expected at least 100 unique traces under {trace_root}; found {len(selected)}")
    return selected


def prepare_guest(rootfs_tree: Path, rootfs: Path) -> None:
    """Install the embedded local runner and rebuild the shared read-only disk."""
    with tempfile.TemporaryDirectory(prefix="swebench-guest-") as temp:
        temp = Path(temp)
        runner, init = temp / "swebench-local-replay.py", temp / "init"
        runner.write_text(GUEST_REPLAY); init.write_text(GUEST_INIT)
        sudo("install", "-m", "0755", str(runner), str(rootfs_tree / "usr/local/bin/swebench-local-replay.py"))
        sudo("install", "-m", "0755", str(init), str(rootfs_tree / "sbin/init"))
        sudo("mkfs.ext4", "-F", "-q", "-d", str(rootfs_tree), str(rootfs))


class VM:
    def __init__(self, run_dir: Path, ram_dir: Path, session: int, manifest: dict, memory_ceiling_mib: int) -> None:
        self.run_dir, self.session, self.manifest = run_dir, session, manifest
        self.vm_dir = run_dir / "vms" / f"session-{session:03d}"; self.vm_dir.mkdir(parents=True)
        self.disk = ram_dir / "disks" / f"session-{session:03d}.raw"
        self.cgroup = Path(f"/sys/fs/cgroup/swebench-microvm-{os.getpid()}-{session}")
        self.memory_ceiling_mib = memory_ceiling_mib
        self.proc: subprocess.Popen | None = None

    def build_disk(self, staging: Path, memory_threshold_bytes: int) -> None:
        image, root = image_name(self.manifest["instance_id"]), staging / f"session-{self.session:03d}"
        root.mkdir(parents=True, exist_ok=False)
        container = subprocess.check_output(["sudo", "-n", "docker", "create", image, "/bin/true"], text=True).strip()
        try:
            exported = subprocess.Popen(["sudo", "-n", "docker", "export", container], stdout=subprocess.PIPE)
            extracted = subprocess.run(["sudo", "-n", "tar", "-C", str(root), "-xpf", "-"], stdin=exported.stdout, check=False)
            assert exported.stdout is not None; exported.stdout.close()
            if exported.wait() or extracted.returncode:
                raise RuntimeError(f"failed to export {image}")
            host_manifest = self.vm_dir / "manifest.json"; host_manifest.write_text(json.dumps(self.manifest, indent=2) + "\n")
            sudo("install", "-D", "-m", "0644", str(host_manifest), str(root / "replay/manifest.json"))
            kib = int(subprocess.check_output(["sudo", "-n", "du", "-sk", str(root)], text=True).split()[0])
            disk_bytes = (kib + 1024 * 1024) * 1024
            if host_used_bytes() + disk_bytes >= memory_threshold_bytes:
                raise MemoryGuard(f"session {self.session} disk would cross the {memory_threshold_bytes} byte host-memory guard")
            self.disk.parent.mkdir(parents=True, exist_ok=True)
            free_tmpfs = shutil.disk_usage(self.disk.parent).free
            if disk_bytes >= free_tmpfs:
                raise MemoryGuard(f"session {self.session} disk needs {disk_bytes} bytes but only {free_tmpfs} bytes remain in {self.disk.parent}")
            call("truncate", "-s", f"{kib + 1024 * 1024}K", str(self.disk))
            sudo("mkfs.ext4", "-F", "-q", "-d", str(root), str(self.disk))
        finally:
            subprocess.run(["sudo", "-n", "docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["sudo", "-n", "rm", "-rf", str(root)], check=True)

    def prepare_cgroup(self) -> None:
        sudo("mkdir", "-p", str(self.cgroup))
        swap_limit = self.cgroup / "memory.swap.max"
        if swap_limit.exists():
            sudo("sh", "-c", f"echo 0 > {swap_limit}")

    def start(self, rootfs: Path, kernel: Path) -> None:
        cpus = cpuset()
        cpu_arg = format_cpu_list(cpus)
        command = ["taskset", "-c", cpu_arg, "numactl", "--interleave=all", "cloud-hypervisor",
                   "--kernel", str(kernel), "--initramfs", f"/boot/initrd.img-{os.uname().release}",
                   "--disk", f"path={rootfs},image_type=raw,readonly=on", "--disk", f"path={self.disk},image_type=raw",
                   "--memory", "size=0", "--memory-zone", f"size={self.memory_ceiling_mib}M,id=mem0",
                   "--numa", f"guest_numa_id=0,cpus={cpu_arg},memory_zones=mem0", "--cpus", f"boot={len(cpus)},max={len(cpus)}",
                   "--console", "off", "--serial", f"file={self.vm_dir / 'serial.log'}",
                   "--cmdline", "root=/dev/vda rootfstype=ext4 rw fsck.mode=skip init=/sbin/init console=ttyS0 panic=-1"]
        self.proc = subprocess.Popen(command, stdout=(self.vm_dir / "vmm.stdout").open("w"), stderr=subprocess.STDOUT, start_new_session=True)
        sudo("sh", "-c", f"echo {self.proc.pid} > {self.cgroup}/cgroup.procs")

    def sample(self) -> dict:
        return {"timestamp": time.time(), "session": self.session, "instance_id": self.manifest["instance_id"],
                "cgroup_memory_bytes": cgroup_value(self.cgroup, "memory.current"), "pss_bytes": pss_bytes(self.cgroup),
                "cpu_usage_usec": cpu_usage_usec(self.cgroup)}

    def done(self) -> bool:
        return self.proc is not None and self.proc.poll() is not None

    def read_jsonl(self, guest_path: str) -> list[dict]:
        try:
            text = subprocess.check_output(["debugfs", "-R", f"cat {guest_path}", str(self.disk)], text=True, stderr=subprocess.DEVNULL)
            return [json.loads(line) for line in text.splitlines() if line]
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return []

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try: self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired: os.killpg(self.proc.pid, signal.SIGKILL)
        try: sudo("rmdir", str(self.cgroup))
        except subprocess.CalledProcessError: pass


def nearest_sample(samples: list[dict], timestamp: float, before: bool) -> dict | None:
    candidates = [row for row in samples if (row["timestamp"] <= timestamp) == before]
    return (max(candidates, key=lambda row: row["timestamp"]) if before else min(candidates, key=lambda row: row["timestamp"], default=None)) if candidates else None


def summarize_turns(vm: VM, samples: list[dict]) -> list[dict]:
    rows = []
    for turn in vm.read_jsonl("/replay/guest-turns.jsonl"):
        first, last = nearest_sample(samples, turn["started_at"], True), nearest_sample(samples, turn["finished_at"], False)
        within = [row for row in samples if turn["started_at"] <= row["timestamp"] <= turn["finished_at"]]
        rows.append({"session": vm.session, "instance_id": vm.manifest["instance_id"], **turn,
                     "cpu_seconds": ((last["cpu_usage_usec"] - first["cpu_usage_usec"]) / 1e6 if first and last else None),
                     "peak_cgroup_memory_bytes": max((r["cgroup_memory_bytes"] for r in within), default=None),
                     "peak_pss_bytes": max((r["pss_bytes"] for r in within), default=None),
                     "sample_count": len(within)})
    return rows


def sessions_for_stage(unique: list[tuple[Path, dict]], count: int, rng: random.Random) -> list[dict]:
    if count <= len(unique):
        return [dict(manifest, repetition=0) for _, manifest in unique[:count]]
    result = [dict(manifest, repetition=0) for _, manifest in unique]
    for _, manifest in rng.choices(unique, k=count - len(unique)):
        copy = dict(manifest); copy["repetition"] = 1; result.append(copy)
    return result


def run_stage(stage_dir: Path, manifests: list[dict], args, rootfs: Path, kernel: Path,
              memory_threshold_bytes: int) -> dict:
    stage_dir.mkdir(parents=True)
    ram_dir = args.ram_root / f"swebench-scale-{os.getpid()}-{len(manifests)}"
    scratch_dir = args.scratch_root / f"swebench-scale-scratch-{os.getpid()}-{len(manifests)}"
    staging = scratch_dir / "staging"; staging.mkdir(parents=True)
    memory_ceiling = args.guest_memory_ceiling_mib or host_memory_bytes() // 1024**2
    vms = [VM(stage_dir, ram_dir, index + 1, manifest, memory_ceiling) for index, manifest in enumerate(manifests)]
    (stage_dir / "run-plan.json").write_text(json.dumps({"concurrency": len(vms), "sessions": [v.manifest for v in vms], "seed": args.seed,
        "guest_visible_vcpus": len(cpuset()), "guest_memory_ceiling_mib": memory_ceiling,
        "resource_policy": "no per-session CPU or memory quota; shared all-NUMA host capacity",
        "host_memory_threshold_percent": args.memory_threshold_percent,
        "host_numa_nodes": [node.name for node in numa_nodes()]}, indent=2) + "\n")
    try:
        for image in sorted({image_name(v.manifest["instance_id"]) for v in vms}):
            if host_used_bytes() >= memory_threshold_bytes:
                raise MemoryGuard("host memory guard reached while preparing images")
            sudo("docker", "pull", image)
        for vm in vms: vm.build_disk(staging, memory_threshold_bytes)
    except BaseException:
        sudo("rm", "-rf", str(ram_dir)); shutil.rmtree(scratch_dir, ignore_errors=True)
        raise
    samples: dict[int, list[dict]] = {vm.session: [] for vm in vms}; stop = threading.Event()
    oom_guard = threading.Event()
    peak_host_used = [host_used_bytes()]
    def monitor() -> None:
        with (stage_dir / "session-samples.jsonl").open("w") as stream:
            while not stop.wait(args.sample_interval_seconds):
                for vm in vms:
                    row = vm.sample(); samples[vm.session].append(row); stream.write(json.dumps(row) + "\n")
                stream.flush()
                used = host_used_bytes()
                peak_host_used[0] = max(peak_host_used[0], used)
                if used >= memory_threshold_bytes:
                    oom_guard.set()
                    stream.write(json.dumps({"timestamp": time.time(), "event": "memory_guard", "host_used_bytes": used,
                                             "threshold_bytes": memory_threshold_bytes}) + "\n")
                    stream.flush()
    for vm in vms: vm.prepare_cgroup()
    monitor_thread = threading.Thread(target=monitor, daemon=True); monitor_thread.start()
    started = time.time()
    try:
        # Start each VMM from a barrier so a stage has one common admission
        # instant, rather than a host-side sequential start ramp.
        launch_gate = threading.Barrier(len(vms))
        launch_errors: list[BaseException] = []
        def launch(vm: VM) -> None:
            try:
                launch_gate.wait()
                vm.start(rootfs, kernel)
            except BaseException as exc:
                launch_errors.append(exc)
        launchers = [threading.Thread(target=launch, args=(vm,)) for vm in vms]
        for thread in launchers: thread.start()
        for thread in launchers: thread.join()
        if launch_errors: raise RuntimeError("failed to launch simultaneous stage") from launch_errors[0]
        while not all(vm.done() for vm in vms):
            if oom_guard.is_set():
                for vm in vms:
                    if not vm.done(): vm.stop()
                break
            time.sleep(0.2)
    finally:
        stop.set(); monitor_thread.join(timeout=2)
        for vm in vms:
            samples[vm.session].append(vm.sample())
            vm.stop()
    turn_rows = [row for vm in vms for row in summarize_turns(vm, samples[vm.session])]
    (stage_dir / "turn-resource.jsonl").write_text("".join(json.dumps(row) + "\n" for row in turn_rows))
    result = {"concurrency": len(vms), "elapsed_seconds": time.time() - started, "sessions_completed": sum(vm.done() for vm in vms),
              "turns_measured": len(turn_rows), "memory_guard_triggered": oom_guard.is_set(),
              "peak_host_used_bytes": peak_host_used[0],
              "peak_aggregate_cgroup_memory_bytes": max((sum(r["cgroup_memory_bytes"] for r in group) for group in zip(*samples.values())), default=0)}
    (stage_dir / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    # Results have been extracted to stage_dir. Reclaim every temporary
    # tmpfs-backed instance disk before preparing the next concurrency point.
    sudo("rm", "-rf", str(ram_dir)); shutil.rmtree(scratch_dir, ignore_errors=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trace-root", type=Path, default=ROOT / "yig")
    parser.add_argument("--stages", type=int, nargs="+", default=DEFAULT_STAGES)
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.25)
    parser.add_argument("--ram-root", type=Path, default=Path("/dev/shm"))
    parser.add_argument("--scratch-root", type=Path, default=Path("/tmp"),
                        help="regular filesystem used while exporting OCI images")
    parser.add_argument("--memory-threshold-percent", type=float, default=90.0)
    parser.add_argument("--guest-memory-ceiling-mib", type=int)
    parser.add_argument("--prepare-guest", action="store_true")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if tuple(args.stages) != DEFAULT_STAGES: raise SystemExit(f"stages must be exactly {DEFAULT_STAGES}")
    if args.sample_interval_seconds <= 0: raise SystemExit("sample interval must be positive")
    if not 1.0 <= args.memory_threshold_percent < 100.0: raise SystemExit("memory threshold must be in [1, 100)")
    rootfs_tree, rootfs = ROOT / "microvm-assets/rootfs-tree", ROOT / "microvm-assets/replay-rootfs.img"
    kernel = Path(os.environ.get("MICROVM_KERNEL", ROOT / "microvm-assets/vmlinuz"))
    unique = load_traces(args.trace_root); rng = random.Random(args.seed); rng.shuffle(unique)
    args.output.mkdir(parents=True, exist_ok=False)
    plan = {"stages": list(args.stages), "unique_traces": len(unique), "seed": args.seed,
            "selection": "distinct sessions within every stage; 128 uses all 100 unique traces plus 28 seeded repeats",
            "guest_execution": "local replay; host has no post-boot guest command channel",
            "memory_threshold_percent": args.memory_threshold_percent,
            "host_numa_nodes": [node.name for node in numa_nodes()],
            "host_cpus": cpuset(),
            "host_memory_bytes": host_memory_bytes()}
    (args.output / "experiment-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    # Rebuild on every execution run so the guest behavior and host-side
    # result parser always match.  `--prepare-guest` supports a separate
    # provisioning-only invocation.
    if args.prepare_guest or args.run: prepare_guest(rootfs_tree, rootfs)
    if not args.run: print(args.output); return
    if not Path("/dev/kvm").exists() or not rootfs.is_file() or not kernel.is_file(): raise SystemExit("--run requires /dev/kvm, a kernel, and a prepared replay rootfs")
    if not all(shutil.which(x) for x in ("cloud-hypervisor", "docker", "debugfs", "numactl")): raise SystemExit("missing cloud-hypervisor, docker, debugfs, or numactl")
    if swap_bytes():
        raise SystemExit("swap is enabled; disable host swap before an in-memory run")
    if filesystem_type(args.ram_root) != "tmpfs":
        raise SystemExit(f"--ram-root must be on tmpfs for an in-memory run (got {filesystem_type(args.ram_root)})")
    memory_threshold_bytes = int(host_memory_bytes() * args.memory_threshold_percent / 100.0)
    results = []
    for count in args.stages:
        if host_used_bytes() >= memory_threshold_bytes:
            results.append({"concurrency": count, "skipped": True, "stop_reason": "host memory guard reached before admission"})
            break
        try:
            result = run_stage(args.output / f"sessions-{count:03d}", sessions_for_stage(unique, count, rng), args, rootfs, kernel, memory_threshold_bytes)
            results.append(result)
            if result.get("memory_guard_triggered"):
                results.append({"skipped_remaining": True, "stop_reason": "host memory guard reached during stage"})
                break
        except MemoryGuard as exc:
            sudo("rm", "-rf", str(args.ram_root / f"swebench-scale-{os.getpid()}-{count}"))
            shutil.rmtree(args.scratch_root / f"swebench-scale-scratch-{os.getpid()}-{count}", ignore_errors=True)
            results.append({"concurrency": count, "skipped": True, "stop_reason": str(exc)})
            break
    (args.output / "experiment-summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__": main()
