#!/usr/bin/env python3
"""Compatibility entry point for autonomous microVM trace replay.

The replay implementation is in ``run_yig_microvm_replay.py``.  It contains
no guest listener: each VM starts a local manifest-driven replay process.
"""
from run_yig_microvm_replay import main


if __name__ == "__main__":
    main()
