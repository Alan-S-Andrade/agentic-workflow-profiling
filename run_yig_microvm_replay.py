#!/usr/bin/env python3
"""Run `/yig` SWE-bench trajectories as autonomous Cloud Hypervisor guests.

Each guest gets an exported official SWE-bench environment on its private
disk, together with a manifest built from one mini-SWE-agent trajectory.  The
guest sleeps through saved model-inference spans and runs the saved shell
commands itself.  The host only starts VMs and samples their cgroups.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
YIG_ROOT = ROOT / "yig"
CPUSET = os.environ.get("MICROVM_CPUSET", "0-15")
KERNEL = Path(os.environ.get("MICROVM_KERNEL", ROOT / "microvm-assets/vmlinuz"))
ROOTFS = Path(os.environ.get("MICROVM_ROOTFS", ROOT / "microvm-assets/replay-rootfs.img"))


def image_name(instance_id: str) -> str:
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_').lower()}:latest"


def sudo(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", *map(str, args)], check=True, text=True, capture_output=True)


def node0_memory_bytes() -> int:
    text = Path("/sys/devices/system/node/node0/meminfo").read_text()
    match = re.search(r"^Node 0 MemTotal:\s+(\d+) kB", text, re.M)
    return int(match.group(1)) * 1024


def read_cpu() -> dict[int, tuple[int, int]]:
    values = {}
    for line in Path("/proc/stat").read_text().splitlines():
        match = re.match(r"cpu(\d+)\s+(.*)", line)
        if match:
            nums = list(map(int, match.group(2).split()))
            values[int(match.group(1))] = (sum(nums), nums[3] + (nums[4] if len(nums) > 4 else 0))
    return values


def cpuset_cpus() -> list[int]:
    """Expand the taskset syntax accepted by the experiment configuration."""
    cpus = []
    for item in CPUSET.split(","):
        if "-" in item:
            first, last = map(int, item.split("-", 1))
            cpus.extend(range(first, last + 1))
        else:
            cpus.append(int(item))
    return sorted(set(cpus))


def cpu_percent(previous: dict[int, tuple[int, int]], current: dict[int, tuple[int, int]]) -> float:
    total = idle = 0
    for cpu in cpuset_cpus():
        if cpu in previous and cpu in current:
            total += current[cpu][0] - previous[cpu][0]
            idle += current[cpu][1] - previous[cpu][1]
    return 100.0 * (1 - idle / total) if total else 0.0


def cgroup_value(path: Path, name: str) -> int:
    try:
        return int((path / name).read_text().strip())
    except (FileNotFoundError, ValueError, PermissionError):
        return 0


def pss_bytes(path: Path) -> int:
    total = 0
    try:
        pids = [int(pid) for pid in (path / "cgroup.procs").read_text().split()]
    except (FileNotFoundError, PermissionError):
        return 0
    for pid in pids:
        try:
            match = re.search(r"^Pss:\s+(\d+) kB", Path(f"/proc/{pid}/smaps_rollup").read_text(), re.M)
            total += int(match.group(1)) * 1024 if match else 0
        except (FileNotFoundError, PermissionError):
            pass
    return total


def trajectory_manifest(path: Path) -> dict:
    data = json.loads(path.read_text())
    instance_id = data["instance_id"]
    assistant_turns = [message for message in data["messages"] if message.get("role") == "assistant" and message.get("tool_calls")]
    timings = data["step_timings"]
    turns = []
    for index, timing in enumerate(timings):
        calls = assistant_turns[index].get("tool_calls", []) if index < len(assistant_turns) else []
        commands = []
        for call in calls:
            function = call.get("function", {})
            if function.get("name") != "bash":
                continue
            try:
                command = json.loads(function["arguments"])["command"]
            except (KeyError, TypeError, json.JSONDecodeError):
                raise ValueError(f"{path}: invalid bash tool-call arguments in turn {index + 1}")
            if not isinstance(command, str):
                raise ValueError(f"{path}: non-string bash command in turn {index + 1}")
            commands.append(command)
        turns.append({
            "turn": timing.get("step", index + 1),
            "inference_seconds": float(timing.get("inference", {}).get("elapsed_s", 0.0)),
            "commands": commands,
        })
    return {
        "instance_id": instance_id,
        "source_trajectory": str(path.relative_to(ROOT)),
        "trajectory_format": data.get("trajectory_format"),
        "turns": turns,
        "unmatched_assistant_tool_turns": max(0, len(assistant_turns) - len(timings)),
    }


def trajectories() -> list[tuple[Path, dict]]:
    found = []
    ids = set()
    for path in sorted(YIG_ROOT.glob("*/results/*/*.traj.json")):
        manifest = trajectory_manifest(path)
        if manifest["instance_id"] in ids:
            continue
        ids.add(manifest["instance_id"])
        found.append((path, manifest))
    if not found:
        raise SystemExit(f"no mini-SWE-agent trajectories found under {YIG_ROOT}")
    return found


class GuestVM:
    def __init__(self, session: int, manifest: dict, output: Path, disk: Path, memory_ceiling_mib: int) -> None:
        self.session = session
        self.manifest = manifest
        self.output = output
        self.disk = disk
        # This is guest-visible addressable memory, not a reservation. Cloud
        # Hypervisor backs guest pages when they are touched, so PSS and
        # cgroup memory grow with the replay's real demand.
        self.memory_ceiling_mib = memory_ceiling_mib
        self.vm_dir = output / "vms" / f"session-{session:04d}"
        self.vm_dir.mkdir(parents=True, exist_ok=True)
        self.cgroup = Path(f"/sys/fs/cgroup/yig-microvm-{os.getpid()}-{session}")
        self.proc: subprocess.Popen | None = None

    def build_disk(self, staging: Path) -> None:
        image = image_name(self.manifest["instance_id"])
        root = staging / f"session-{self.session:04d}"
        root.mkdir(parents=True, exist_ok=False)
        container = subprocess.check_output(["sudo", "-n", "docker", "create", image, "/bin/true"], text=True).strip()
        try:
            exported = subprocess.Popen(["sudo", "-n", "docker", "export", container], stdout=subprocess.PIPE)
            extracted = subprocess.run(["sudo", "-n", "tar", "-C", str(root), "-xpf", "-"], stdin=exported.stdout, check=False)
            assert exported.stdout is not None
            exported.stdout.close()
            if exported.wait() or extracted.returncode:
                raise RuntimeError(f"failed to export {image}")
            replay = root / "replay"
            # Docker export restores a root-owned filesystem, so stage the
            # manifest outside it and install it with the image's ownership.
            host_manifest = self.vm_dir / "manifest.json"
            host_manifest.write_text(json.dumps(self.manifest, indent=2) + "\n")
            sudo("install", "-D", "-m", "0644", str(host_manifest), str(replay / "manifest.json"))
            kib = int(subprocess.check_output(["sudo", "-n", "du", "-sk", str(root)], text=True).split()[0])
            self.disk.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["truncate", "-s", f"{kib + 1024 * 1024}K", str(self.disk)], check=True)
            sudo("mkfs.ext4", "-F", "-q", "-d", str(root), str(self.disk))
        finally:
            subprocess.run(["sudo", "-n", "docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["sudo", "-n", "rm", "-rf", str(root)], check=True)

    def start(self) -> None:
        sudo("mkdir", "-p", str(self.cgroup))
        command = [
            "taskset", "-c", CPUSET, "numactl", "--membind=0", "cloud-hypervisor",
            "--kernel", str(KERNEL), "--initramfs", f"/boot/initrd.img-{os.uname().release}",
            "--disk", f"path={ROOTFS},image_type=raw,readonly=on",
            "--disk", f"path={self.disk},image_type=raw",
            "--memory", "size=0", "--memory-zone", f"size={self.memory_ceiling_mib}M,host_numa_node=0,id=mem0",
            "--numa", f"guest_numa_id=0,cpus=0-{len(cpuset_cpus()) - 1},memory_zones=mem0",
            # Every guest sees the whole host Node-0 CPU set. There is no
            # cgroup cpu.max, so guest CPU consumption expands and contracts
            # with runnable work under normal host scheduling competition.
            "--cpus", f"boot={len(cpuset_cpus())},max={len(cpuset_cpus())}",
            "--console", "off", "--serial", f"file={self.vm_dir / 'serial.log'}",
            "--cmdline", "root=/dev/vda rootfstype=ext4 rw fsck.mode=skip init=/sbin/init console=ttyS0 panic=-1",
        ]
        self.proc = subprocess.Popen(command, stdout=(self.vm_dir / "vmm.stdout").open("w"), stderr=subprocess.STDOUT, start_new_session=True)
        sudo("sh", "-c", f"echo {self.proc.pid} > {self.cgroup}/cgroup.procs")

    def sample(self) -> dict:
        return {
            "session": self.session,
            "instance_id": self.manifest["instance_id"],
            "cgroup_memory_bytes": cgroup_value(self.cgroup, "memory.current"),
            "pss_bytes": pss_bytes(self.cgroup),
        }

    def finished(self) -> bool:
        return self.proc is not None and self.proc.poll() is not None

    def collect(self) -> dict:
        try:
            raw = subprocess.check_output(["debugfs", "-R", "cat /replay/summary.json", str(self.disk)], text=True, stderr=subprocess.DEVNULL)
            return json.loads(raw)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return {"instance_id": self.manifest["instance_id"], "guest_summary": "unavailable"}

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        try:
            sudo("rmdir", str(self.cgroup))
        except subprocess.CalledProcessError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sessions", type=int, default=None, help="default: every unique trajectory in yig")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--batch-interval-seconds", type=float, default=20.0)
    parser.add_argument("--guest-memory-ceiling-mib", type=int, default=None,
                        help="guest-visible memory ceiling; default is all Node-0 RAM and is not preallocated")
    parser.add_argument("--cpu-stop-percent", type=float, default=95.0)
    parser.add_argument("--ram-root", type=Path, default=Path("/dev/shm"))
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    selected = trajectories()
    if args.sessions is not None:
        selected = selected[:args.sessions]
    if not selected or args.batch_size < 1 or args.batch_interval_seconds < 0:
        raise SystemExit("sessions and batch-size must be positive; interval cannot be negative")
    memory_ceiling_mib = args.guest_memory_ceiling_mib or node0_memory_bytes() // 1024**2
    if memory_ceiling_mib < 1:
        raise SystemExit("guest-memory-ceiling-mib must be positive")
    args.output.mkdir(parents=True, exist_ok=False)
    plan = {
        "benchmark": "SWE-bench_Verified",
        "trajectory_root": str(YIG_ROOT),
        "sessions": [manifest["instance_id"] for _, manifest in selected],
        "admission": {"batch_size": args.batch_size, "batch_interval_seconds": args.batch_interval_seconds},
        "guest_execution": "local manifest-driven replay; no host-to-guest command channel",
        "llm_mode": "guest sleep using recorded inference_seconds",
        "guest_memory_ceiling_mib": memory_ceiling_mib,
        "per_vm_memory_limit": "none; the host admission threshold is global",
        "cpuset": CPUSET,
        "guest_visible_vcpus": len(cpuset_cpus()),
        "per_vm_cpu_limit": "none; all VMs share the configured Node-0 CPU set",
    }
    (args.output / "runner-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    if not args.run:
        print(args.output)
        return
    if not Path("/dev/kvm").exists() or not KERNEL.is_file() or not ROOTFS.is_file():
        raise SystemExit("--run requires /dev/kvm, kernel, and rebuilt local-replay rootfs")
    if not shutil.which("cloud-hypervisor") or not shutil.which("docker"):
        raise SystemExit("--run requires cloud-hypervisor and docker")

    ram_dir = args.ram_root / f"yig-microvm-replay-{os.getpid()}"
    staging = ram_dir / "staging"
    disks = ram_dir / "disks"
    staging.mkdir(parents=True)
    disks.mkdir()
    active: list[GuestVM] = []
    completed = []
    stop_reason = "all scheduled sessions admitted"
    stop_admission = threading.Event()
    monitor_stop = threading.Event()
    start = time.monotonic()
    memory_limit = int(node0_memory_bytes() * 0.90)

    def aggregate() -> tuple[int, int]:
        samples = [vm.sample() for vm in active]
        return sum(s["cgroup_memory_bytes"] for s in samples), sum(s["pss_bytes"] for s in samples)

    def monitor() -> None:
        previous = read_cpu()
        with (args.output / "workload-samples.jsonl").open("w") as stream:
            while not monitor_stop.wait(1.0):
                current = read_cpu()
                memory, pss = aggregate()
                row = {"timestamp": time.time(), "elapsed_seconds": time.monotonic() - start,
                       "resident_sessions": len(active), "aggregate_memory_bytes": memory,
                       "aggregate_pss_bytes": pss, "node0_cpu_percent": cpu_percent(previous, current)}
                stream.write(json.dumps(row) + "\n")
                stream.flush()
                previous = current
                if memory >= memory_limit or row["node0_cpu_percent"] >= args.cpu_stop_percent:
                    stop_admission.set()

    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    try:
        for offset in range(0, len(selected), args.batch_size):
            scheduled = start + (offset // args.batch_size) * args.batch_interval_seconds
            while time.monotonic() < scheduled:
                time.sleep(min(0.2, scheduled - time.monotonic()))
            if stop_admission.is_set():
                stop_reason = "memory or CPU admission threshold reached"
                break
            batch = selected[offset:offset + args.batch_size]
            vms = [GuestVM(offset + index + 1, manifest, args.output, disks / f"session-{offset + index + 1:04d}.raw", memory_ceiling_mib)
                   for index, (_, manifest) in enumerate(batch)]
            for vm in vms:
                image = image_name(vm.manifest["instance_id"])
                sudo("docker", "pull", image)
                vm.build_disk(staging)
            for vm in vms:
                vm.start()
                active.append(vm)
            still_active = []
            for vm in active:
                if vm.finished():
                    completed.append(vm.collect())
                    vm.stop()
                else:
                    still_active.append(vm)
            active[:] = still_active
        while active:
            still_active = []
            for vm in active:
                if vm.finished():
                    completed.append(vm.collect())
                    vm.stop()
                else:
                    still_active.append(vm)
            active[:] = still_active
            time.sleep(0.2)
    finally:
        monitor_stop.set()
        monitor_thread.join(timeout=2)
        for vm in active:
            vm.stop()
    (args.output / "completed-sessions.json").write_text(json.dumps(completed, indent=2) + "\n")
    (args.output / "summary.json").write_text(json.dumps({
        "scheduled_sessions": len(selected), "completed_sessions": len(completed),
        "stop_reason": stop_reason, "guest_execution": plan["guest_execution"],
        "no_remote_llm_api_calls": True,
    }, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
