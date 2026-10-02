#!/usr/bin/env python3
"""Compatibility entry point for autonomous microVM PSS measurement.

The `/yig` runner continuously writes aggregate cgroup memory and PSS to
`workload-samples.jsonl`; no guest inspection channel is required.
"""
from run_yig_microvm_replay import main


if __name__ == "__main__":
    main()
