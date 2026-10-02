#!/usr/bin/env python3
"""Create the admission plan for a bounded SWE-bench Verified microVM sweep.

The plan deliberately has no dependency on the Hugging Face ``datasets``
package.  Materialize the official ``test`` split to JSONL first, then pass it
here.  Keeping the manifest alongside the run makes the precise instance order
and batch schedule reproducible.
"""
import argparse
import json
from pathlib import Path


def load_instances(path: Path) -> list[str]:
    ids = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        instance_id = row.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise SystemExit(f"{path}:{line_number}: missing string instance_id")
        ids.append(instance_id)
    if len(set(ids)) != len(ids):
        raise SystemExit("instance manifest contains duplicate instance_id values")
    if not ids:
        raise SystemExit("instance manifest is empty")
    return ids


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("instances", type=Path,
                        help="JSONL materialization of the official Verified test split")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-instances", type=int, default=500,
                        help="number of distinct benchmark instances to schedule")
    parser.add_argument("--turn-cap", type=int, default=25)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--batch-interval-seconds", type=float, default=20.0)
    args = parser.parse_args()
    if args.max_instances < 1 or args.turn_cap < 1 or args.batch_size < 1:
        raise SystemExit("max-instances, turn-cap, and batch-size must be positive")
    if args.batch_interval_seconds < 0:
        raise SystemExit("batch-interval-seconds cannot be negative")

    instance_ids = load_instances(args.instances)[:args.max_instances]
    sessions = []
    for offset, instance_id in enumerate(instance_ids):
        batch = offset // args.batch_size
        sessions.append({
            "session": offset + 1,
            "instance_id": instance_id,
            "admission_batch": batch + 1,
            "admit_at_seconds": batch * args.batch_interval_seconds,
            "turn_cap": args.turn_cap,
        })
    plan = {
        "benchmark": "SWE-bench_Verified",
        "split": "test",
        "instance_count": len(sessions),
        "session_lifecycle": "start one VM, run one bounded instance, destroy VM on completion",
        "terminology": {
            "session": "one VM sandbox running one benchmark instance",
            "turn": "one mini-SWE-agent model response",
            "step": "one Bash command emitted in a turn",
        },
        "limits": {"turn_cap": args.turn_cap},
        "admission": {
            "batch_size": args.batch_size,
            "batch_interval_seconds": args.batch_interval_seconds,
            "policy": "admit each batch concurrently; do not retain completed sessions",
        },
        "tenant_sessions": sessions,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
