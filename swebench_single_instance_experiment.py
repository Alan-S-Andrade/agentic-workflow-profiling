#!/usr/bin/env python3
"""Run one representative remote SWE-bench microVM and measure its timing.

The remote inference service exposes instance IDs but not trajectory summaries.
This wrapper therefore walks every catalog trajectory once, using a private
session per instance, and selects the instance closest to the catalog means for
both turn count and the sum of server-reported inference durations.  It then
delegates the actual VM run to ``swebench_microvm_scale_experiment`` and writes
the VM-stage and per-turn timings into the requested output directory.

The profiling pass does not execute guest commands; it only reads the saved
remote turn responses.  The selected instance is then executed once in a
microVM, with the existing runner's artifact, network, cgroup, and cleanup
behavior.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import swebench_microvm_scale_experiment as scale


DEFAULT_ENDPOINT = scale.DEFAULT_INFERENCE_ENDPOINT


def get_json(url: str, timeout: float) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"request failed for {url}: {exc}") from exc


def post_json(url: str, payload: dict, timeout: float) -> dict:
    encoded = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"request failed: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("inference response must be a JSON object")
    return value


def catalog_url(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rsplit("/", 1)[0] + "/instances", "", "")
    )


def instance_ids(endpoint: str, timeout: float) -> list[str]:
    payload = get_json(catalog_url(endpoint), timeout)
    if isinstance(payload, dict):
        payload = payload.get("instances", payload.get("instance_ids"))
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise SystemExit("remote instance catalog must be a JSON list or an object with an 'instances' list")
    values = list(dict.fromkeys(payload))
    if not values:
        raise SystemExit("remote instance catalog is empty")
    return values


def profile_instance(endpoint: str, instance_id: str, timeout: float,
                     max_turns: int) -> dict:
    session = f"single-instance-profile-{uuid.uuid4().hex}"
    turn = 1
    inference_seconds = 0.0
    probe_started = time.monotonic()
    while turn <= max_turns:
        response = post_json(endpoint, {
            "instance_id": instance_id,
            "session_id": session,
            "turn": turn,
        }, timeout)
        returned_turn = response.get("turn", turn)
        if returned_turn != turn:
            raise RuntimeError(f"instance {instance_id} returned turn {returned_turn}, expected {turn}")
        raw_duration = response.get("inference_seconds", 0.0)
        try:
            inference_seconds += float(raw_duration or 0.0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"instance {instance_id} returned invalid inference_seconds") from exc
        if response.get("done", False):
            return {
                "instance_id": instance_id,
                "turns": turn,
                "trace_inference_seconds": inference_seconds,
                "profile_probe_seconds": time.monotonic() - probe_started,
            }
        turn += 1
    raise RuntimeError(f"instance {instance_id} exceeded --profile-max-turns={max_turns}")


def choose_representative(profiles: list[dict]) -> tuple[dict, dict]:
    mean_turns = statistics.fmean(item["turns"] for item in profiles)
    mean_duration = statistics.fmean(item["trace_inference_seconds"] for item in profiles)
    turn_scale = mean_turns or 1.0
    duration_scale = mean_duration or 1.0
    ranked = []
    for item in profiles:
        score = (
            abs(item["turns"] - mean_turns) / turn_scale
            + abs(item["trace_inference_seconds"] - mean_duration) / duration_scale
        )
        ranked.append((score, item["instance_id"], item))
    _, _, selected = min(ranked)
    return selected, {
        "mean_turns": mean_turns,
        "mean_trace_inference_seconds": mean_duration,
        "selection_score": min(ranked)[0],
        "selection_rule": "minimize relative distance from both catalog means; ties use instance_id",
    }


def run_microvm(endpoint: str, instance_id: str, output: Path,
                allow_swap: bool, numa_node: int | None,
                compute_numa_node: int | None,
                memory_numa_node: int | None,
                migrate_at_turn: int | None,
                migration_from_node: int | None,
                migration_to_node: int | None) -> float:
    if output.exists():
        raise SystemExit(f"output directory already exists: {output}")
    original_stages = scale.DEFAULT_STAGES
    scale.DEFAULT_STAGES = (1,)
    argv = [
        "swebench_microvm_scale_experiment.py",
        "--output", str(output),
        "--inference-endpoint", endpoint,
        "--instance-id", instance_id,
        "--stages", "1",
        "--run",
    ]
    if numa_node is not None:
        argv.extend(["--numa-node", str(numa_node)])
    if compute_numa_node is not None:
        argv.extend(["--compute-numa-node", str(compute_numa_node)])
    if memory_numa_node is not None:
        argv.extend(["--memory-numa-node", str(memory_numa_node)])
    if migrate_at_turn is not None:
        argv.extend(["--migrate-at-turn", str(migrate_at_turn)])
    if migration_from_node is not None:
        argv.extend(["--migration-from-node", str(migration_from_node)])
    if migration_to_node is not None:
        argv.extend(["--migration-to-node", str(migration_to_node)])
    if allow_swap:
        argv.append("--allow-swap")
    old_argv = sys.argv
    started = time.monotonic()
    try:
        sys.argv = argv
        scale.main()
    finally:
        sys.argv = old_argv
        scale.DEFAULT_STAGES = original_stages
    return time.monotonic() - started


def write_report(output: Path, endpoint: str, profiles: list[dict],
                 selected: dict, selection: dict, profile_elapsed: float,
                 wrapper_elapsed: float) -> None:
    stage_path = output / "sessions-001" / "summary.json"
    turn_path = output / "sessions-001" / "turn-resource.jsonl"
    if not stage_path.is_file() or not turn_path.is_file():
        raise SystemExit(f"microVM run did not produce timing files under {output}")
    stage = json.loads(stage_path.read_text())
    turns = []
    for line in turn_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        row["turn_elapsed_seconds"] = row["finished_at"] - row["started_at"]
        turns.append(row)
    report = {
        "instance_id": selected["instance_id"],
        "inference_endpoint": endpoint,
        "selection": selection,
        "selected_profile": selected,
        "catalog_profile_count": len(profiles),
        "catalog_profiles": profiles,
        "timing": {
            "catalog_profile_elapsed_seconds": profile_elapsed,
            "client_wrapper_elapsed_seconds": wrapper_elapsed,
            "microvm_end_to_end_seconds": stage["elapsed_seconds"],
            "turns_measured": len(turns),
            "turn_elapsed_seconds_sum": sum(row["turn_elapsed_seconds"] for row in turns),
        },
        "microvm_summary": stage,
        "turns": turns,
    }
    (output / "single-instance-report.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "turn-timings.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in turns)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="new output directory for the selected one-VM run")
    parser.add_argument("--inference-endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--profile-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--profile-max-turns", type=int, default=1000)
    parser.add_argument("--allow-swap", action="store_true",
                        help="pass --allow-swap to the VM runner")
    parser.add_argument("--numa-node", type=int,
                        help="legacy shorthand: bind compute and sandbox memory to this node")
    parser.add_argument("--compute-numa-node", type=int,
                        help="host NUMA node for vCPU threads and VMM computation")
    parser.add_argument("--memory-numa-node", type=int,
                        help="host NUMA node for the guest sandbox memory")
    parser.add_argument("--migrate-at-turn", type=int,
                        help="migrate the running sandbox after this completed turn")
    parser.add_argument("--migrate-midpoint", action="store_true",
                        help="migrate after the selected instance's midpoint turn")
    parser.add_argument("--migration-from-node", type=int,
                        help="source NUMA node for migration")
    parser.add_argument("--migration-to-node", type=int,
                        help="destination NUMA node for migration")
    args = parser.parse_args()
    if args.profile_timeout_seconds <= 0 or args.profile_max_turns <= 0:
        raise SystemExit("profile timeout and max turns must be positive")
    scale.validate_inference_endpoint(args.inference_endpoint)

    profile_started = time.monotonic()
    profiles = []
    failures = []
    for instance_id in instance_ids(args.inference_endpoint, args.profile_timeout_seconds):
        try:
            profiles.append(profile_instance(
                args.inference_endpoint, instance_id,
                args.profile_timeout_seconds, args.profile_max_turns,
            ))
        except RuntimeError as exc:
            failures.append({"instance_id": instance_id, "error": str(exc)})
    if not profiles:
        raise SystemExit(f"could not profile any remote instance: {failures}")
    profile_elapsed = time.monotonic() - profile_started
    selected, selection = choose_representative(profiles)
    migrate_at_turn = args.migrate_at_turn
    if args.migrate_midpoint:
        if migrate_at_turn is not None:
            raise SystemExit("use only one of --migrate-at-turn and --migrate-midpoint")
        migrate_at_turn = max(1, selected["turns"] // 2)
    if migrate_at_turn is not None and args.migration_to_node is None:
        raise SystemExit("--migration-to-node is required when migration is requested")
    wrapper_started = time.monotonic()
    run_microvm(args.inference_endpoint, selected["instance_id"], args.output,
                args.allow_swap, args.numa_node,
                args.compute_numa_node, args.memory_numa_node,
                migrate_at_turn, args.migration_from_node,
                args.migration_to_node)
    wrapper_elapsed = time.monotonic() - wrapper_started
    write_report(args.output, args.inference_endpoint, profiles, selected, selection,
                 profile_elapsed, wrapper_elapsed)
    if failures:
        (args.output / "profile-failures.json").write_text(json.dumps(failures, indent=2) + "\n")
    print(args.output)
    print(json.dumps({"selected": selected, "selection": selection}, indent=2))


if __name__ == "__main__":
    main()
