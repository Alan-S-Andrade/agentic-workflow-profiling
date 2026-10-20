#!/usr/bin/env python3
"""Enable the research TPP kernel ABI; never migrate a process in userspace."""

import argparse
import json
import os
import platform
import re
import sys
from pathlib import Path


CONTROLS = {
    "balancing": "proc/sys/kernel/numa_balancing",
    "demotion": "sys/kernel/mm/numa/demotion_enabled",
    "watermark": "proc/sys/vm/demote_scale_factor",
}


def inspect(root=Path("/")):
    """Read-only capability and topology checks for the PACT TPP v5.15 patch."""
    errors = []
    values = {}
    for name, relative in CONTROLS.items():
        path = root / relative
        if not path.is_file():
            errors.append(f"Missing /{relative}; boot the research TPP patched kernel.")
        else:
            values[name] = path.read_text().strip()
    zoneinfo = (root / "proc/zoneinfo").read_text()
    zones = re.findall(
        r"Node (\d+), zone\s+\w+, toptier (\d+) next_demotion_node (-?\d+)",
        zoneinfo,
    )
    if not zones:
        errors.append("Missing TPP zoneinfo topology ABI; ordinary NUMA balancing is insufficient.")
    elif not any(n == "0" and top == "1" and target == "1" for n, top, target in zones):
        errors.append("Kernel does not report node 0 as top tier with demotion target node 1.")
    normal = re.search(
        r"Node 0, zone\s+Normal, toptier 1 next_demotion_node 1(.*?)(?=\nNode |\Z)",
        zoneinfo, re.S,
    )
    watermark = managed = None
    if normal:
        demote = re.search(r"^\s*demote\s+(\d+)", normal[1], re.M)
        pages = re.search(r"^\s*managed\s+(\d+)", normal[1], re.M)
        watermark = int(demote[1]) if demote else None
        managed = int(pages[1]) if pages else None
    if not managed:
        errors.append("Research TPP requires populated Normal-zone memory on fast node 0.")
    for node in (0, 1):
        path = root / f"sys/devices/system/node/node{node}/meminfo"
        if not path.is_file() or not re.search(r"MemTotal:\s+[1-9]\d*", path.read_text()):
            errors.append(f"Node {node} must exist with memory online.")
    cpulist = root / "sys/devices/system/node/node0/cpulist"
    if not cpulist.is_file() or not cpulist.read_text().strip():
        errors.append("Fast node 0 must have online CPUs.")
    return {"kernel": platform.release(), "controls": values,
            "zones": sorted(set(zones)), "fast_normal_demote_pages": watermark,
            "fast_normal_managed_pages": managed, "errors": errors}


def write_checked(path, value):
    path.write_text(value + "\n")
    actual = path.read_text().strip()
    if actual != value:
        raise RuntimeError(f"{path}: requested {value!r}, read back {actual!r}")


def enable(scale, root=Path("/")):
    report = inspect(root)
    if report["errors"]:
        raise RuntimeError("\n".join(report["errors"]))
    # The TPP watermark handler only calculates demotion watermarks while
    # tiered promotion is active. Write it LAST, even if its value is unchanged.
    changes = [("demotion", "true"), ("balancing", "2"), ("watermark", str(scale))]
    attempted = []
    try:
        for name, value in changes:
            attempted.append(name)
            write_checked(root / CONTROLS[name], value)
        if not inspect(root)["fast_normal_demote_pages"]:
            raise RuntimeError("TPP demotion watermark is still zero after enabling.")
    except Exception as error:
        failures = []
        for name in reversed(attempted):
            try:
                write_checked(root / CONTROLS[name], report["controls"][name])
            except Exception as rollback_error:
                failures.append(str(rollback_error))
        raise RuntimeError(f"Enable failed: {error}. Rollback errors: {failures}") from error
    return report["controls"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "enable"))
    parser.add_argument("--demote-scale-factor", type=int, default=200,
                        help="demotion watermark in 1/10000 units (default 200 = 2%%)")
    args = parser.parse_args()
    if not 1 <= args.demote_scale_factor <= 10000:
        parser.error("--demote-scale-factor must be between 1 and 10000")
    try:
        report = inspect()
        if args.action == "enable":
            if os.geteuid() != 0:
                parser.error("enable requires root; use sudo")
            report["previous_controls"] = enable(args.demote_scale_factor)
            report.update(inspect())
            report["enabled"] = True
        print(json.dumps(report, indent=2))
        return 1 if report["errors"] else 0
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
