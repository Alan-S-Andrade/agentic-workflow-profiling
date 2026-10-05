#!/usr/bin/env python3
"""Force the kernel to reclaim file page cache sitting in the GPU's HBM (NUMA node 1).

Run under `numactl --membind=1` before vLLM starts: touching almost all of node 1's
reclaimable memory makes the kernel drop clean page cache there (including other users'
leftovers, which posix_fadvise cannot reach); the memory is released on exit.
"""
import mmap, re, sys

node = int(sys.argv[1]) if len(sys.argv) > 1 else 1
info = {}
for line in open(f"/sys/devices/system/node/node{node}/meminfo"):
    m = re.match(r"Node \d+ (\S+):\s+(\d+)", line)
    if m:
        info[m.group(1)] = int(m.group(2)) * 1024
target = info["MemFree"] + info.get("FilePages", 0) - (2 << 30)  # keep 2 GiB headroom
if info.get("FilePages", 0) < (512 << 20) or target <= 0:
    print(f"node{node}: FilePages {info.get('FilePages', 0) >> 20} MiB, nothing to flush")
    sys.exit(0)
chunk = b"\1" * (64 << 20)
m = mmap.mmap(-1, target)
for _ in range(target // len(chunk)):
    m.write(chunk)
m.close()
print(f"node{node}: touched {target >> 30} GiB to reclaim {info['FilePages'] >> 20} MiB of page cache")
