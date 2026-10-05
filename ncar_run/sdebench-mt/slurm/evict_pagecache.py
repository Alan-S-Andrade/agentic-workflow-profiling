#!/usr/bin/env python3
"""Drop clean page-cache pages of the given files/dirs (posix_fadvise DONTNEED; no root needed).

On GH200 the GPU's HBM is also NUMA node 1, so file cache that spills there shows up as used
GPU memory and shrinks vLLM's KV cache. Usage: evict_pagecache.py PATH...
"""
import os, sys

n = 0
for root in sys.argv[1:]:
    paths = [root] if os.path.isfile(root) else [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs]
    for p in paths:
        try:
            fd = os.open(p, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            n += 1
        finally:
            os.close(fd)
print(f"evicted page cache of {n} files")
