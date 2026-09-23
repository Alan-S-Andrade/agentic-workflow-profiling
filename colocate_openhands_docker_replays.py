#!/usr/bin/env python3
"""Run many persistent no-LLM Docker sessions and sample their footprint.

Run this script as root (normally ``sudo python3 ...``). Every recorded replay
replay owns one labelled Docker sandbox that remains resident during its
recorded LLM waits and optional post-workflow idle hold. CPU is sampled every
10 ms by default; PSS is sampled less frequently because smaps is costly.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path


RUNNING = True


def stop(*_args):
    global RUNNING
    RUNNING = False


def read_proc(pid: int):
    proc = Path("/proc") / str(pid)
    try:
        stat = (proc / "stat").read_text()
        end = stat.rfind(")")
        fields = stat[end + 2:].split()
        return {
            "pid": pid, "ppid": int(fields[1]), "utime": int(fields[11]),
            "stime": int(fields[12]), "start_ticks": int(fields[19]),
        }
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None


def all_procs():
    result = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and (info := read_proc(int(entry.name))):
            result[info["pid"]] = info
    return result


def descendants(roots, processes):
    known = set(roots)
    changed = True
    while changed:
        changed = False
        for info in processes.values():
            if info["ppid"] in known and info["pid"] not in known:
                known.add(info["pid"])
                changed = True
    return known


def docker_containers(label: str):
    listed = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"label={label}"],
        text=True, capture_output=True, check=False,
    )
    ids = listed.stdout.split()
    if not ids:
        return set()
    inspected = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}} {{.State.Pid}}", *ids],
        text=True, capture_output=True, check=False,
    )
    result = []
    for line in inspected.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) > 0:
            result.append((parts[0], int(parts[1])))
    return result


def cgroup_path_for_pid(pid: int) -> Path | None:
    try:
        for line in (Path("/proc") / str(pid) / "cgroup").read_text().splitlines():
            hierarchy, _, path = line.partition("::")
            if hierarchy == "0" and path:
                return Path("/sys/fs/cgroup") / path.lstrip("/")
    except OSError:
        pass
    return None


def cgroup_stats(pid: int):
    root = cgroup_path_for_pid(pid)
    if not root:
        return None
    try:
        memory_current = int((root / "memory.current").read_text())
        # Some cgroup-v2 deployments do not expose memory.peak. The sampler
        # maintains its own peak, so lack of this optional kernel file must
        # not suppress current Docker/memcg accounting.
        peak_file = root / "memory.peak"
        memory_peak = int(peak_file.read_text()) if peak_file.exists() else memory_current
        cpu = {key: int(value) for key, value in
               (line.split() for line in (root / "cpu.stat").read_text().splitlines())}
        memory = {key: int(value) for key, value in
                  (line.split() for line in (root / "memory.stat").read_text().splitlines())}
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None
    return {"memory_current": memory_current, "memory_peak": memory_peak,
            "cpu_usage_usec": cpu.get("usage_usec", 0), "memory_anon": memory.get("anon", 0),
            "memory_file": memory.get("file", 0), "memory_slab": memory.get("slab", 0)}


def memory_stats(pid: int):
    values = {"pss": 0, "pss_anon": 0, "pss_file": 0, "pss_shmem": 0,
              "shared_clean": 0, "shared_dirty": 0}
    names = {"Pss:": "pss", "Pss_Anon:": "pss_anon", "Pss_File:": "pss_file",
             "Pss_Shmem:": "pss_shmem", "Shared_Clean:": "shared_clean",
             "Shared_Dirty:": "shared_dirty"}
    try:
        for line in (Path("/proc") / str(pid) / "smaps_rollup").read_text().splitlines():
            for prefix, key in names.items():
                if line.startswith(prefix):
                    values[key] = int(line.split()[1]) * 1024
                    break
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None
    return values


def meminfo():
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.split()[0]) * 1024
    return values


def host_cpu_ticks():
    fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    return sum(map(int, fields)), sum(map(int, fields[:3]))


def selected_cpu_ticks(cpus: list[int] | None):
    if not cpus:
        return host_cpu_ticks()
    wanted = {f"cpu{cpu}" for cpu in cpus}
    total = busy = 0
    for line in Path("/proc/stat").read_text().splitlines():
        fields = line.split()
        if fields and fields[0] in wanted:
            ticks = list(map(int, fields[1:]))
            total += sum(ticks)
            busy += sum(ticks[:3])
    return total, busy


def parse_cpu_list(value: str | None) -> list[int] | None:
    if not value:
        return None
    cpus = []
    for item in value.split(","):
        if "-" in item:
            start, end = map(int, item.split("-", 1))
            cpus.extend(range(start, end + 1))
        else:
            cpus.append(int(item))
    return sorted(set(cpus))


def numa_mem_total(node: int) -> int:
    path = Path(f"/sys/devices/system/node/node{node}/meminfo")
    if not path.exists():
        raise SystemExit(f"NUMA node does not exist: {node}")
    for line in path.read_text().splitlines():
        if "MemTotal:" in line:
            match = re.search(r"(\d+)\s+kB", line)
            if match:
                return int(match.group(1)) * 1024
    raise SystemExit(f"could not read MemTotal for NUMA node {node}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_dir", type=Path)
    parser.add_argument("--source-workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--interval-ms", type=float, default=10)
    parser.add_argument("--pss-interval-ms", type=float, default=100)
    parser.add_argument("--cpu-stop-percent", type=float, default=90,
                        help="stop after sustained host CPU percentage of all cores")
    parser.add_argument("--memory-stop-percent", type=float, default=85,
                        help="stop when replay PSS reaches this percentage of host RAM")
    parser.add_argument("--sustain-samples", type=int, default=100,
                        help="number of CPU samples required to declare saturation")
    parser.add_argument("--image", default="openhands-trace-replay:go1.24")
    parser.add_argument("--idle-hold-seconds", type=float, default=300,
                        help="retain completed session sandboxes to measure idle-resident capacity")
    parser.add_argument("--cpuset-cpus", help="CPU list passed to every persistent Docker sandbox")
    parser.add_argument("--cpuset-mems", help="NUMA memory-node list passed to every persistent Docker sandbox")
    parser.add_argument("--capacity-cpus", help="CPU list used for the CPU saturation threshold; defaults to host-wide")
    parser.add_argument("--capacity-memory-node", type=int,
                        help="NUMA node whose physical DRAM defines the PSS saturation threshold")
    parser.add_argument("--sandbox-command", default="exec sleep infinity",
                        help="resident command passed to each session container")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run with sudo so PSS is readable for Docker container processes")
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {args.output}")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be positive")
    if args.idle_hold_seconds < 0:
        raise SystemExit("--idle-hold-seconds must be non-negative")
    args.output.mkdir(parents=True)
    label = f"openhands.colocation={uuid.uuid4().hex}"
    interval, pss_interval = args.interval_ms / 1000, args.pss_interval_ms / 1000
    hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    initial_mem = meminfo()
    capacity_memory = (numa_mem_total(args.capacity_memory_node) if args.capacity_memory_node is not None
                       else initial_mem["MemTotal"])
    memory_limit = capacity_memory * args.memory_stop_percent / 100
    capacity_cpus = parse_cpu_list(args.capacity_cpus)
    host_cpu_limit = args.cpu_stop_percent
    roots, processes = [], []
    for index in range(1, args.concurrency + 1):
        workspace = args.output / f"workspace-{index:03d}"
        shutil.copytree(args.source_workspace, workspace, symlinks=True)
        replay_out = args.output / f"replay-{index:03d}"
        command = [
            "python3", str(Path(__file__).with_name("replay_openhands_trace.py")), str(args.trace_dir),
            "--workspace", str(workspace), "--output", str(replay_out), "--runtime", "docker",
            "--docker-image", args.image, "--container-label", label,
            "--idle-hold-seconds", str(args.idle_hold_seconds),
            "--sandbox-command", args.sandbox_command,
        ]
        if args.cpuset_cpus:
            command += ["--cpuset-cpus", args.cpuset_cpus]
        if args.cpuset_mems:
            command += ["--cpuset-mems", args.cpuset_mems]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append((index, process, workspace, replay_out))
        roots.append(process.pid)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    started = time.time()
    next_pss, previous_cpu, previous_capacity_cpu, sustained, stop_reason = started, {}, None, 0, None
    with (args.output / "samples.jsonl").open("w", encoding="utf-8") as stream:
        while RUNNING and any(process.poll() is None for _, process, _, _ in processes):
            observed = time.time()
            process_table = all_procs()
            controller_pids = descendants(roots, process_table)
            # Only the persistent session container (and processes it owns)
            # contributes to capacity PSS. The per-session replay controller
            # remains included in CPU accounting but not the sandbox model.
            containers = docker_containers(label)
            sandbox_roots = {pid for _, pid in containers}
            sandbox_pids = descendants(sandbox_roots, process_table)
            pids = controller_pids | sandbox_pids
            cpu_ms = 0.0
            for pid in pids:
                info = process_table.get(pid)
                if not info:
                    continue
                key = (pid, info["start_ticks"])
                current = info["utime"] + info["stime"]
                if key in previous_cpu:
                    cpu_ms += (current - previous_cpu[key]) / hz * 1000
                previous_cpu[key] = current
            total_ticks, busy_ticks = selected_cpu_ticks(capacity_cpus)
            capacity_cpu = None
            if previous_capacity_cpu:
                total_delta, busy_delta = total_ticks - previous_capacity_cpu[0], busy_ticks - previous_capacity_cpu[1]
                capacity_cpu = busy_delta / total_delta * 100 if total_delta else 0
            previous_capacity_cpu = (total_ticks, busy_ticks)
            pss = None
            memory = None
            cgroups = None
            if observed >= next_pss:
                memory = {key: 0 for key in ("pss", "pss_anon", "pss_file", "pss_shmem", "shared_clean", "shared_dirty")}
                for pid in sandbox_pids:
                    if stats := memory_stats(pid):
                        for key, value in stats.items():
                            memory[key] += value
                pss = memory["pss"]
                cgroups = {key: 0 for key in ("memory_current", "memory_peak", "cpu_usage_usec",
                                               "memory_anon", "memory_file", "memory_slab")}
                for _, pid in containers:
                    if stats := cgroup_stats(pid):
                        for key, value in stats.items():
                            cgroups[key] += value
                next_pss = observed + pss_interval
            record = {
                "observed_at": observed, "elapsed_ms": round((observed - started) * 1000, 3),
                "observed_pid_count": len(pids), "sandbox_pid_count": len(sandbox_pids),
                "replay_cpu_ms": round(cpu_ms, 3),
                "replay_cpu_percent": cpu_ms / (interval * 1000) * 100,
                "capacity_cpu_percent": capacity_cpu, "replay_pss_bytes": pss,
                "mem_available_bytes": meminfo()["MemAvailable"],
            }
            if memory:
                # Shared_* is an RSS mapping total and must not be added to
                # PSS for capacity. It is recorded to expose sharing.
                record.update({f"replay_{key}_bytes": value for key, value in memory.items() if key != "pss"})
            if cgroups:
                # These are the same cgroup-v2 counters surfaced by Docker
                # stats. Unlike PSS, they include charged page cache and
                # kernel memory, which is what drives memcg capacity/OOM.
                record.update({f"docker_{key}_bytes" if key != "cpu_usage_usec" else "docker_cpu_usage_usec": value
                               for key, value in cgroups.items()})
            stream.write(json.dumps(record) + "\n")
            stream.flush()
            if capacity_cpu is not None and capacity_cpu >= host_cpu_limit:
                sustained += 1
            else:
                sustained = 0
            if sustained >= args.sustain_samples:
                stop_reason = "sustained_host_cpu"
                break
            if cgroups is not None and cgroups["memory_current"] >= memory_limit:
                stop_reason = "docker_cgroup_memory_limit"
                break
            time.sleep(interval)

    if stop_reason:
        for _, process, _, _ in processes:
            if process.poll() is None:
                process.terminate()
    results = []
    for index, process, workspace, replay_out in processes:
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        results.append({"replica": index, "exit_status": process.returncode,
                        "workspace": str(workspace), "replay_output": str(replay_out),
                        "stdout": stdout, "stderr": stderr})
    samples = [json.loads(line) for line in (args.output / "samples.jsonl").read_text().splitlines()]
    summary = {
        "started_at": started, "ended_at": time.time(), "concurrency": args.concurrency,
        "container_label": label, "sampling": {"cpu_interval_ms": args.interval_ms, "pss_interval_ms": args.pss_interval_ms},
        "cpuset": {"cpus": args.cpuset_cpus, "mems": args.cpuset_mems},
        "capacity_scope": {"cpus": args.capacity_cpus, "memory_node": args.capacity_memory_node,
                           "memory_total_bytes": capacity_memory},
        "thresholds": {"capacity_cpu_percent": host_cpu_limit,
                       "docker_memory_current_bytes": memory_limit},
        "stop_reason": stop_reason or "all_replays_completed",
        "peak": {
            "capacity_cpu_percent": max((sample["capacity_cpu_percent"] or 0 for sample in samples), default=0),
            "replay_cpu_ms_per_interval": max((sample["replay_cpu_ms"] for sample in samples), default=0),
            "replay_cpu_percent": max((sample["replay_cpu_percent"] for sample in samples), default=0),
            "replay_pss_bytes": max((sample["replay_pss_bytes"] or 0 for sample in samples), default=0),
            "docker_memory_current_bytes": max((sample.get("docker_memory_current_bytes") or 0 for sample in samples), default=0),
            "docker_memory_peak_bytes": max((sample.get("docker_memory_peak_bytes") or 0 for sample in samples), default=0),
            "docker_memory_anon_bytes": max((sample.get("docker_memory_anon_bytes") or 0 for sample in samples), default=0),
            "docker_memory_file_bytes": max((sample.get("docker_memory_file_bytes") or 0 for sample in samples), default=0),
            "observed_pid_count": max((sample["observed_pid_count"] for sample in samples), default=0),
        },
        "replicas": results,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
