#!/usr/bin/env python3
"""Drive a single OpenHands conversation through several human turns."""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def request(base_url: str, api_key: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={"X-Session-API-Key": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def events(base_url: str, api_key: str, conversation_id: str) -> list[dict]:
    path = f"/api/conversations/{conversation_id}/events/search?" + urllib.parse.urlencode(
        {"limit": 100, "sort_order": "TIMESTAMP"}
    )
    return request(base_url, api_key, "GET", path).get("items", [])


def wait_for_turn(
    base_url: str, api_key: str, conversation_id: str, before: set[str], timeout: int, stall_timeout: int
) -> tuple[list[dict], str]:
    deadline = time.monotonic() + timeout
    last_count, quiet_since, last_progress = -1, None, time.monotonic()
    while time.monotonic() < deadline:
        current = [event for event in events(base_url, api_key, conversation_id) if event.get("id") not in before]
        made_progress = len(current) != last_count
        if made_progress:
            last_count, last_progress = len(current), time.monotonic()
        has_terminal_response = any(
            (event.get("kind") == "MessageEvent" and event.get("source") == "agent")
            or (event.get("kind") == "ActionEvent" and (event.get("action") or {}).get("kind") == "FinishAction")
            for event in current
        )
        terminal_error = any(
            event.get("kind") == "ConversationStateUpdateEvent"
            and event.get("key") == "execution_status"
            and event.get("value") == "error"
            for event in current
        )
        if terminal_error:
            return current, "error"
        if has_terminal_response:
            if made_progress:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= 3:
                return current, "finished"
        if time.monotonic() - last_progress >= stall_timeout:
            return current, "stalled"
        time.sleep(1)
    return current, "timeout"


def create_conversation(base_url: str, session_key: str, workspace: Path, llm: dict) -> str:
    conversation_id = str(uuid.uuid4())
    created = request(
        base_url,
        session_key,
        "POST",
        "/api/conversations",
        {
            "conversation_id": conversation_id,
            "workspace": {"kind": "LocalWorkspace", "working_dir": str(workspace)},
            "worktree": False,
            # Some terminal-only inspections require several exploratory
            # actions plus a final FinishAction. Six avoids treating that
            # internal loop cap as a failed human turn.
            "max_iterations": 6,
            "stuck_detection": True,
            "autotitle": True,
            "confirmation_policy": {"kind": "NeverConfirm"},
            "agent_settings": {
                "agent_kind": "openhands",
                "llm": llm,
                "condenser": {"enabled": False},
                "tools": [{"name": "terminal", "params": {}}],
            },
        },
    )
    if not created.get("id"):
        raise RuntimeError(f"conversation creation failed: {created}")
    return conversation_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--llm-base-url", default="http://127.0.0.1:18766/v1",
                        help="OpenAI-compatible inference endpoint (normally the trace proxy)")
    parser.add_argument("--session-key", default=os.environ.get("OPENHANDS_SESSION_KEY"),
                        help="Agent-server session key (or OPENHANDS_SESSION_KEY environment variable)")
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--stall-timeout", type=int, default=20)
    parser.add_argument("--max-recoveries", type=int, default=2)
    parser.add_argument("--counter-root-pid", type=int, help="agent-server root PID to profile for every turn")
    parser.add_argument("--turn-count", type=int, default=None, help="Run only the first N scripted human turns")
    args = parser.parse_args()
    if not args.session_key:
        raise SystemExit("Provide --session-key or OPENHANDS_SESSION_KEY")
    api_key = os.environ["OPENAI_API_KEY"]
    args.workspace.mkdir(parents=True, exist_ok=True)
    llm = {
        "model": "openai/gpt-5.6-luna",
        "api_key": api_key,
        "base_url": args.llm_base_url,
        "max_output_tokens": 800,
        "temperature": 0,
    }
    conversation_id = create_conversation(args.base_url, args.session_key, args.workspace, llm)
    conversation_ids = [conversation_id]
    turns = [
        "Inspect push/processor.go. Report only the exported function and method names; do not edit.",
        "Search the repository for callers of NewProcessor. Report only the count; do not edit.",
        "Inspect push/processor_unit_test.go. List its top-level test function names only; do not edit.",
        "Run go test ./push -run TestProcessorCreation. Report pass or fail only; do not edit.",
        "Read the body of TestProcessorCreation and report whether it checks for a non-nil registry; do not edit.",
        "Rerun go test ./push -run TestProcessorCreation. Report pass or fail only.",
        "Search this repository for ErrPushNotificationNameTooLong. Report matching file paths only; do not edit.",
        "Read the processPendingNotifications function in push/processor.go and summarize its nil-reader behavior in one sentence; do not edit.",
        "Read the nil-reader guard in processPendingNotifications and report the returned value; do not edit.",
        "Run go test ./push -run TestProcessPendingNotificationsNilReader. Report pass or fail only; do not edit.",
        "Run git status --short and report changed paths only; do not edit.",
        "Read the imports in push/processor.go and report whether any are unused; do not edit.",
        "Run go vet ./push. Report pass or fail only; do not edit.",
        "Read the comment immediately above the nil-reader guard and report its first five words; do not edit.",
        "Run go vet ./push. Report pass or fail only; do not edit.",
        "Search push/ for processPendingNotifications. Report matching paths only; do not edit.",
        "Read one other occurrence of ProcessPendingNotifications in push/. In one sentence, state whether it delegates to the shared implementation; do not edit.",
        "Run go test ./push -run TestProcessorCreation. Report pass or fail only; do not edit.",
        "Summarize the two inspected code areas in exactly two sentences; do not edit.",
        "Run git status --short. Report changed paths only; do not edit.",
    ]
    if args.turn_count is not None:
        if not 1 <= args.turn_count <= len(turns):
            raise SystemExit(f"--turn-count must be between 1 and {len(turns)}")
        turns = turns[: args.turn_count]
    record_path = args.trace_dir / "turns.jsonl"
    final_status, recorded_turns, recovery_count = "finished", 0, 0
    attempt_path = args.trace_dir / "turn-attempts.jsonl"
    recovery_path = args.trace_dir / "session-recoveries.jsonl"
    event_path = args.trace_dir / "agent-events.jsonl"
    # Keep an append-only, timestamped copy of *every* event returned by the
    # agent server.  ``turns.jsonl`` is convenient for turn-level analysis,
    # but this stream is the replay source for non-LLM actions.
    with record_path.open("w", encoding="utf-8") as record, attempt_path.open("w", encoding="utf-8") as attempts, recovery_path.open("w", encoding="utf-8") as recoveries, event_path.open("w", encoding="utf-8") as event_stream:
        for index, text in enumerate(turns, start=1):
            logical_started, all_attempts = time.time(), []
            for attempt in range(1, args.max_recoveries + 2):
                before = {event.get("id") for event in events(args.base_url, args.session_key, conversation_id)}
                started, profiler = time.time(), None
                if args.counter_root_pid:
                    profiler = subprocess.Popen([sys.executable, str(Path(__file__).with_name("turn_hardware_profile.py")), "--root-pid", str(args.counter_root_pid), "--turn-id", str(index), "--output", str(args.trace_dir / f"hardware-turn-{index:02d}-attempt-{attempt}.json")])
                request(args.base_url, args.session_key, "POST", f"/api/conversations/{conversation_id}/events", {"role": "user", "content": [{"type": "text", "text": text}], "run": True})
                try:
                    turn_events, turn_status = wait_for_turn(args.base_url, args.session_key, conversation_id, before, args.timeout, args.stall_timeout)
                finally:
                    if profiler:
                        profiler.terminate()
                        profiler.wait(timeout=10)
                attempt_record = {"turn_id": index, "attempt": attempt, "conversation_id": conversation_id, "started_at": started, "ended_at": time.time(), "status": turn_status, "events": turn_events}
                for event in turn_events:
                    event_stream.write(json.dumps({
                        "turn_id": index,
                        "attempt": attempt,
                        "conversation_id": conversation_id,
                        "observed_at": time.time(),
                        "event": event,
                    }) + "\n")
                event_stream.flush()
                all_attempts.append(attempt_record)
                attempts.write(json.dumps(attempt_record) + "\n")
                attempts.flush()
                if turn_status == "finished":
                    record.write(json.dumps({"turn_id": index, "conversation_id": conversation_id, "started_at": logical_started, "ended_at": attempt_record["ended_at"], "input": text, "status": "finished", "event_count": sum(len(item["events"]) for item in all_attempts), "events": turn_events, "attempts": all_attempts}) + "\n")
                    record.flush()
                    recorded_turns += 1
                    break
                if attempt > args.max_recoveries:
                    final_status = turn_status
                    record.write(json.dumps({"turn_id": index, "conversation_id": conversation_id, "started_at": logical_started, "ended_at": attempt_record["ended_at"], "input": text, "status": turn_status, "event_count": sum(len(item["events"]) for item in all_attempts), "events": turn_events, "attempts": all_attempts}) + "\n")
                    record.flush()
                    recorded_turns += 1
                    break
                prior_conversation = conversation_id
                conversation_id = create_conversation(args.base_url, args.session_key, args.workspace, llm)
                conversation_ids.append(conversation_id)
                recovery_count += 1
                recoveries.write(json.dumps({"turn_id": index, "attempt": attempt, "reason": turn_status, "from_conversation_id": prior_conversation, "to_conversation_id": conversation_id, "workspace": str(args.workspace)}) + "\n")
                recoveries.flush()
            if final_status != "finished":
                break
    (args.trace_dir / "conversation.json").write_text(
        json.dumps({"conversation_id": conversation_ids[0], "conversation_ids": conversation_ids, "workspace": str(args.workspace), "model": "gpt-5.6-luna", "requested_turn_count": len(turns), "recorded_turn_count": recorded_turns, "recovery_count": recovery_count, "status": final_status}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(conversation_id)


if __name__ == "__main__":
    main()
