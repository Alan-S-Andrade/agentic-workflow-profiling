#!/usr/bin/env python3
"""Replay an OpenHands trace without calling an LLM.

For Docker replay, one long-lived container is the mutable execution
environment of one session.  Terminal actions use ``docker exec`` into that
same container; recorded LLM spans are host-side waits while the container
remains resident.  This models the persistent Coder/Terminal sandbox in the
Copilot production study instead of creating an ephemeral container per tool
action.
"""

import argparse
from datetime import datetime
import json
import os
import shutil
import subprocess
import time
from pathlib import Path


def jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def timestamp(event: dict, fallback: float) -> float:
    for key in ("timestamp", "created_at", "createdAt", "time"):
        value = event.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                try:
                    # Agent-server events use ISO 8601 while the proxy writes
                    # Unix seconds. Normalize both onto one replay timeline.
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
    return fallback


def terminal_command(event: dict) -> str | None:
    action = event.get("action") or {}
    if action.get("kind") != "TerminalAction":
        return None
    for key in ("command", "command_line", "commandLine"):
        value = action.get(key)
        if isinstance(value, str):
            return value
    return None


def proc_tree(roots: set[int]) -> set[int]:
    """Return roots plus descendants, tolerating short-lived tool processes."""
    table = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            table[int(entry.name)] = int(stat[stat.rfind(")") + 2:].split()[1])
        except (OSError, IndexError, ValueError):
            continue
    result, changed = set(roots), True
    while changed:
        changed = False
        for pid, ppid in table.items():
            if ppid in result and pid not in result:
                result.add(pid)
                changed = True
    return result


def sandbox_snapshot(docker_prefix: list[str], container: str) -> dict:
    """Boundary telemetry: PSS plus the cgroup-v2 counters used by Docker."""
    inspected = subprocess.run(docker_prefix + ["inspect", "--format", "{{.State.Pid}}", container],
                               text=True, capture_output=True, check=False)
    if inspected.returncode or not inspected.stdout.strip().isdigit():
        return {"available": False}
    pid = int(inspected.stdout.strip())
    pids = proc_tree({pid})
    pss = 0
    for child in pids:
        try:
            for line in (Path("/proc") / str(child) / "smaps_rollup").read_text().splitlines():
                if line.startswith("Pss:"):
                    pss += int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError):
            pass
    result = {"available": True, "pid_count": len(pids), "pss_bytes": pss}
    try:
        cgroup_rel = next(line.split("::", 1)[1] for line in
                           (Path("/proc") / str(pid) / "cgroup").read_text().splitlines()
                           if line.startswith("0::"))
        root = Path("/sys/fs/cgroup") / cgroup_rel.lstrip("/")
        result["docker_memory_current_bytes"] = int((root / "memory.current").read_text())
        peak_file = root / "memory.peak"
        result["docker_memory_peak_bytes"] = (int(peak_file.read_text()) if peak_file.exists()
                                               else result["docker_memory_current_bytes"])
        result["docker_cpu_usage_usec"] = int(next(line.split()[1] for line in
                                                     (root / "cpu.stat").read_text().splitlines()
                                                     if line.startswith("usage_usec")))
    except (OSError, StopIteration, ValueError):
        result["cgroup_available"] = False
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_dir", type=Path)
    parser.add_argument("--workspace", type=Path, required=True,
                        help="durable replay workspace; it must already be a copy of source-revision")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--speed", type=float, default=1.0,
                        help="scale recorded LLM waits; 1 preserves observed duration")
    parser.add_argument("--runtime", choices=("docker", "namespace", "none"), default="docker",
                        help="isolation runtime for every recorded terminal step")
    parser.add_argument("--docker-image", default="openhands-trace-replay:go1.24",
                        help="image used when --runtime=docker")
    parser.add_argument("--container-label", help="Docker label applied to the persistent session container")
    parser.add_argument("--cpuset-cpus", help="Docker CPU set for the persistent sandbox (for example 0,2,4)")
    parser.add_argument("--cpuset-mems", help="Docker NUMA memory-node set for the persistent sandbox (for example 0)")
    parser.add_argument("--tmpfs", action="append", default=[],
                        help="tmpfs mount specification passed to Docker; may be repeated")
    parser.add_argument("--sandbox-command", default="exec sleep infinity",
                        help="long-lived command that provides the session's resident agent runtime")
    parser.add_argument("--idle-hold-seconds", type=float, default=0,
                        help="keep the completed session sandbox resident for this many seconds")
    parser.add_argument("--loop", action="store_true",
                        help="repeat the recorded trace until this replay process is stopped")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.speed < 0:
        raise SystemExit("--speed must be non-negative")
    if args.idle_hold_seconds < 0:
        raise SystemExit("--idle-hold-seconds must be non-negative")
    source = args.trace_dir.resolve()
    output = args.output or source.with_name(source.name + "-container-replay")
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {output}")
    if not args.workspace.is_dir():
        raise SystemExit(f"workspace does not exist: {args.workspace}")
    output.mkdir(parents=True)

    turns = jsonl(source / "turns.jsonl")
    llms = jsonl(source / "llm.jsonl")
    events = jsonl(source / "agent-events.jsonl")
    conversation = json.loads((source / "conversation.json").read_text(encoding="utf-8"))
    captured_workspace = conversation.get("workspace")
    if not events:
        # Backward-compatible fallback: successful logical turns contain the
        # same raw events, although without individual observation timestamps.
        events = [
            {"turn_id": turn["turn_id"], "attempt": 1, "event": event,
             "observed_at": turn["ended_at"]}
            for turn in turns for event in turn.get("events", [])
        ]

    by_turn = {turn["turn_id"]: turn for turn in turns}
    steps = []
    for item in events:
        event = item["event"]
        command = terminal_command(event)
        if command is not None:
            steps.append({"kind": "terminal", "turn_id": item["turn_id"],
                          "recorded_at": timestamp(event, item["observed_at"]),
                          "command": command, "event_id": event.get("id")})
    for span in llms:
        # Associate a proxy span with the enclosing external user turn.
        matched = next((turn for turn in turns if turn["started_at"] <= span["started_at"] <= turn["ended_at"]), None)
        if matched:
            steps.append({"kind": "llm_wait", "turn_id": matched["turn_id"],
                          "recorded_at": span["started_at"], "duration_ms": span["duration_ms"],
                          "span_id": span.get("span_id"), "status": span.get("status")})
    steps.sort(key=lambda step: (step["turn_id"], step["recorded_at"], step["kind"] != "llm_wait"))

    namespace_runner = shutil.which("unshare")
    docker_runner = shutil.which("docker")
    if args.runtime == "docker" and docker_runner is None:
        raise SystemExit("--runtime=docker requires Docker")
    if args.runtime == "namespace" and namespace_runner is None:
        raise SystemExit("--runtime=namespace requires unshare")
    docker_prefix = ["sudo", docker_runner] if args.runtime == "docker" and os.geteuid() != 0 else [docker_runner]
    manifest = {
        "source_trace": str(source), "workspace": str(args.workspace.resolve()),
        "execution_mode": ("persistent-docker-session-container" if args.runtime == "docker"
                           else f"{args.runtime}-scripted-container" if args.runtime != "none"
                           else "durable-local-workspace"),
        "container_runtime": docker_runner if args.runtime == "docker" else None,
        "container_image": args.docker_image if args.runtime == "docker" else None,
        "container_label": args.container_label,
        "cpuset_cpus": args.cpuset_cpus, "cpuset_mems": args.cpuset_mems,
        "tmpfs": args.tmpfs,
        "sandbox_command": args.sandbox_command,
        "llm_mode": "recorded-duration-wait", "llm_wait_scale": args.speed,
        "turn_count": len(by_turn), "step_count": len(steps),
        "limitations": [
            "Docker mode has one persistent container per replay session; terminal commands run through docker exec in it.",
            "LLM responses are not regenerated or injected; waits model their measured wall time.",
        ],
    }
    (output / "replay-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    session_container = None
    if args.runtime == "docker" and not args.dry_run:
        create = docker_prefix + ["run", "-d", "--rm", "--init", "--network", "none", "--workdir", "/workspace",
                                  "--env", "GIT_CONFIG_COUNT=1", "--env", "GIT_CONFIG_KEY_0=safe.directory",
                                  "--env", "GIT_CONFIG_VALUE_0=/workspace",
                                  "--mount", f"type=bind,src={args.workspace.resolve()},dst=/workspace"]
        if args.container_label:
            create += ["--label", args.container_label]
        if args.cpuset_cpus:
            create += ["--cpuset-cpus", args.cpuset_cpus]
        if args.cpuset_mems:
            create += ["--cpuset-mems", args.cpuset_mems]
        for mount in args.tmpfs:
            create += ["--tmpfs", mount]
        create += [args.docker_image, "bash", "-lc", args.sandbox_command]
        created = subprocess.run(create, text=True, capture_output=True, check=False)
        if created.returncode:
            raise SystemExit(f"could not start persistent session container: {created.stderr.strip()}")
        session_container = created.stdout.strip()
        manifest["session_container"] = session_container
        (output / "replay-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    try:
        with (output / "steps.jsonl").open("w", encoding="utf-8") as record:
          iteration = 0
          while True:
            iteration += 1
            for order, step in enumerate(steps, start=1):
                started = time.time()
                result = {**step, "order": order, "iteration": iteration, "started_at": started}
                if step["kind"] == "llm_wait":
                    wait_seconds = step["duration_ms"] / 1000 * args.speed
                    if not args.dry_run:
                        # The persistent tool sandbox deliberately remains alive
                        # throughout this interval, just as it does during agent
                        # inference.  There is no LLM call and no wait container.
                        if wait_seconds:
                            time.sleep(wait_seconds)
                        result["container_command"] = "persistent sandbox retained during mocked LLM wait"
                    result["mocked"] = True
                elif args.dry_run:
                    result["dry_run"] = True
                else:
                    shell_command = step["command"]
                # OpenHands sometimes writes an absolute LocalWorkspace path
                # into its terminal command.  Inside Docker that exact path is
                # represented by the durable bind mount instead.
                    if captured_workspace:
                        shell_command = shell_command.replace(captured_workspace, "/workspace")
                    result["container_command"] = shell_command
                    command = ["bash", "-c", shell_command]
                    if args.runtime == "docker":
                        command = docker_prefix + [
                            "exec", "--workdir", "/workspace",
                            "--env", "GIT_CONFIG_COUNT=1", "--env", "GIT_CONFIG_KEY_0=safe.directory",
                            "--env", "GIT_CONFIG_VALUE_0=/workspace",
                            session_container,
                        ] + command
                    elif args.runtime == "namespace":
                        command = [namespace_runner, "--user", "--map-root-user", "--mount", "--pid", "--net", "--fork", "--mount-proc", "--"] + command
                    completed = subprocess.run(
                        command, cwd=args.workspace,
                        text=True, capture_output=True, check=False,
                    )
                    result.update({"exit_status": completed.returncode,
                                   "stdout": completed.stdout, "stderr": completed.stderr})
                result["ended_at"] = time.time()
                result["duration_ms"] = round((result["ended_at"] - started) * 1000, 3)
                if session_container and not args.dry_run:
                    result["session_metrics"] = sandbox_snapshot(docker_prefix, session_container)
                record.write(json.dumps(result) + "\n")
                record.flush()
            if not args.loop:
                break
        if args.idle_hold_seconds and not args.dry_run:
            # Model a session whose agent has stopped issuing actions while its
            # mutable sandbox, processes, and mapped pages remain reclaimable.
            time.sleep(args.idle_hold_seconds)
    finally:
        if session_container:
            subprocess.run(docker_prefix + ["rm", "-f", session_container], text=True,
                           capture_output=True, check=False)
    # Per-turn aggregates deliberately retain boundary PSS and cgroup peaks.
    # The colocator's 100 ms samples complement these with burst observations.
    step_records = jsonl(output / "steps.jsonl")
    per_turn = []
    for turn_id in sorted({record["turn_id"] for record in step_records}):
        records = [record for record in step_records if record["turn_id"] == turn_id]
        metrics = [record.get("session_metrics", {}) for record in records if record.get("session_metrics", {}).get("available")]
        per_turn.append({
            "turn_id": turn_id, "step_count": len(records),
            "boundary_peak_pss_bytes": max((m.get("pss_bytes", 0) for m in metrics), default=0),
            "boundary_peak_docker_memory_bytes": max((m.get("docker_memory_current_bytes", 0) for m in metrics), default=0),
            "cgroup_memory_peak_bytes": max((m.get("docker_memory_peak_bytes", 0) for m in metrics), default=0),
            "last_docker_cpu_usage_usec": max((m.get("docker_cpu_usage_usec", 0) for m in metrics), default=0),
        })
    (output / "per-turn.json").write_text(json.dumps(per_turn, indent=2) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
