#!/usr/bin/env python3
"""Capture host and Linux-isolation evidence for one workflow run."""

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time


def read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def namespaces(pid: int) -> dict[str, str]:
    result = {}
    for name in ("pid", "mnt", "net", "user", "cgroup", "uts", "ipc", "time"):
        try:
            result[name] = os.readlink(f"/proc/{pid}/ns/{name}")
        except OSError:
            continue
    return result


def command_output(command: list[str]) -> str | None:
    if not shutil.which(command[0]):
        return None
    try:
        return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--deployment", help="declared deployment mode, if known")
    args = parser.parse_args()

    cgroup_type = command_output(["stat", "-fc", "%T", "/sys/fs/cgroup"])
    pid1_cgroup = read_text("/proc/1/cgroup")
    markers = [path for path in ("/.dockerenv", "/run/.containerenv") if Path(path).exists()]
    cpuinfo = read_text("/proc/cpuinfo") or ""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "captured_at": time.time(),
                "workflow": args.workflow,
                "declared_deployment": args.deployment,
                "hostname": platform.node(),
                "kernel": platform.platform(),
                "cgroup_filesystem": cgroup_type,
                "self_cgroup": read_text("/proc/self/cgroup"),
                "pid1_cgroup": pid1_cgroup,
                "self_namespaces": namespaces(os.getpid()),
                "pid1_namespaces": namespaces(1),
                "container_markers": markers,
                "systemd_detect_virt": command_output(["systemd-detect-virt"]),
                "cpu_hypervisor_flag": "hypervisor" in cpuinfo,
                "interpretation": "Namespace/cgroup differences or a container marker are evidence of container isolation. "
                "A hypervisor flag or systemd-detect-virt result is host-level VM evidence, not proof that an individual step ran in a VM.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
