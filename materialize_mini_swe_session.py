#!/usr/bin/env python3
"""Create the executable per-turn prompt manifest for a multi-step SWE session."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def main():
    session = json.loads((ROOT / "mini_swe_20turn_session.json").read_text())
    template = (ROOT / "prompt_for_mini_swe_feature_episode.txt").read_text()
    output = ROOT / "session_turns.jsonl"
    with output.open("w") as fp:
        for spec in session["turns"]:
            record = dict(spec)
            record["session_id"] = session["session_id"]
            record["semantic_turn_definition"] = session["semantic_turn_definition"]
            record["prompt"] = template.format(**spec)
            record["inference_budget"] = 14
            record["expected_workflow"] = ["inspect", "implement", "compile", "test", "regression_test", "document", "validate"]
            fp.write(json.dumps(record) + "\n")
    print(output)

if __name__ == "__main__":
    main()
