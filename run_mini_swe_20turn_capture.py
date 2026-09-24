#!/usr/bin/env python3
"""Capture a 20-feature SWE-agent session without modifying the user's target."""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "session_turns.jsonl"
BASE_TARGET = ROOT / "swe-default-target"
TARGET = ROOT / "targets" / "mini-swe-session-target"

def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)

def main():
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY must be set")
    rows = [json.loads(x) for x in MANIFEST.read_text().splitlines() if x]
    if len(rows) != 20:
        raise SystemExit("session manifest must contain exactly 20 turns")
    if TARGET.exists():
        raise SystemExit(f"Refusing to reuse existing generated target: {TARGET}")
    TARGET.parent.mkdir(exist_ok=True)
    run(["git", "-C", str(BASE_TARGET), "worktree", "add", "--detach", str(TARGET), "8010edc7"])
    session_dir = ROOT / "traces" / f"mini-swe-20turn-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(json.dumps({"turn_count": 20, "model": os.environ.get("WORKFLOW_MODEL"), "reasoning_effort": os.environ.get("REASONING_EFFORT"), "target": str(TARGET)}, indent=2) + "\n")
    try:
        for spec in rows:
            # Preserve process, package, and tool caches while restoring only
            # the generated source worktree to the common historical baseline.
            run(["git", "-C", str(TARGET), "reset", "--hard", "8010edc7"])
            run(["git", "-C", str(TARGET), "clean", "-ffd"])
            env = os.environ.copy()
            env["SWE_TARGET"] = str(TARGET)
            completed = subprocess.run([str(ROOT / "profile-run.sh"), "swe-agent", spec["prompt"]], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            trace = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else None
            record = {"turn": spec["turn"], "title": spec["title"], "commit": spec["commit"], "exit_status": completed.returncode, "trace": trace}
            with (session_dir / "turns.jsonl").open("a") as out:
                out.write(json.dumps(record) + "\n")
            print(json.dumps(record), flush=True)
            if completed.returncode:
                raise SystemExit(completed.returncode)
    finally:
        # The worktree is generated solely for this experiment; leave neither
        # its feature edits nor its checkout behind.
        subprocess.run(["git", "-C", str(BASE_TARGET), "worktree", "remove", "--force", str(TARGET)], check=False)
    print(session_dir)

if __name__ == "__main__":
    main()
