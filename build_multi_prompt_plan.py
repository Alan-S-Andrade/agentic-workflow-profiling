#!/usr/bin/env python3
"""Turn a measured single-prompt replay into a duration-matched prompt plan.

This is a systems workload: extra prompts mark new user arrivals, while the
retained YIG tool turns remain in their original order. It does not claim that
the new prompts are part of the original SWE-bench task or verify a new patch.
"""

import argparse
import copy
import json
import math
import random
from collections import defaultdict
from pathlib import Path


def spaced_indices(length, count):
    if count == 1:
        return [0]
    return sorted({round(i * (length - 1) / (count - 1)) for i in range(count)})


def make_plan(stages, fraction, max_prompts, min_wait, seed, gap_weights,
              min_sessions=0, min_multi_prompt_sessions=0):
    sessions = []
    seen = set()
    for stage in stages:
        summary = json.loads((stage / "summary.json").read_text())
        run_plan = json.loads((stage / "run-plan.json").read_text())
        turns = defaultdict(list)
        for line in (stage / "turn-resource.jsonl").open():
            row = json.loads(line)
            turns[int(row["session"])].append(row)
        for baseline_number, manifest in enumerate(run_plan["sessions"], 1):
            instance_id = manifest["instance_id"]
            duration = summary["session_workflows"].get(str(baseline_number), {}).get("elapsed_seconds")
            rows = sorted(turns[baseline_number], key=lambda row: int(row["turn"]))
            if instance_id in seen or duration is None or not rows:
                continue
            seen.add(instance_id)
            number = len(sessions) + 1
            sessions.append((number, instance_id, stage, baseline_number, float(duration), rows))
    planned = []
    for number, instance_id, stage, baseline_number, duration, rows in sessions:
        duration = float(duration)
        # Short replays cannot contain observable user waits at this budget.
        prompts = min(max_prompts, 1 + int(duration >= 2) + int(duration >= 5)
                      + int(duration >= 10))
        kept_count = (len(rows) if prompts == 1 else
                      min(len(rows), max(prompts, math.ceil(len(rows) * fraction))))
        while True:
            indices = spaced_indices(len(rows), kept_count)
            selected = [rows[i] for i in indices]
            active = sum(max(0.0, row["finished_at"] - row["started_at"])
                         for row in selected)
            if active + min_wait * (prompts - 1) <= duration or kept_count <= prompts:
                if active + min_wait * (prompts - 1) <= duration or prompts == 1:
                    break
                prompts -= 1
                kept_count = max(kept_count, prompts)
            else:
                kept_count -= 1
        if prompts == 1:
            selected = rows
            active = sum(max(0.0, row["finished_at"] - row["started_at"])
                         for row in selected)
        # The waits use Azure gap samples as relative weights, scaled to the
        # exact time remaining after estimated retained work.
        budget = max(0.0, duration - active)
        rng = random.Random(seed + number)
        weights = [rng.choice(gap_weights) for _ in range(prompts - 1)]
        total_weight = sum(weights)
        extra_wait = max(0.0, budget - min_wait * (prompts - 1))
        waits = ([min_wait + extra_wait * weight / total_weight for weight in weights]
                 if total_weight else [])
        episodes = []
        for prompt_index in range(prompts):
            first = round(prompt_index * len(selected) / prompts)
            last = round((prompt_index + 1) * len(selected) / prompts)
            episodes.append({
                "prompt_index": prompt_index + 1,
                "prompt_text": ("Initial user request" if prompt_index == 0
                                else "Continue working on the same request"),
                "wait_before_seconds": 0.0 if prompt_index == 0 else waits[prompt_index - 1],
                "source_turns": [int(row["turn"]) for row in selected[first:last]],
            })
        planned.append({
            "session": number, "instance_id": instance_id,
            "baseline_stage": str(stage), "baseline_session": baseline_number,
            "target_elapsed_seconds": duration,
            "source_turn_count": len(rows), "retained_turn_count": len(selected),
            "estimated_retained_active_seconds": active,
            "planned_user_wait_seconds": sum(waits),
            "estimated_total_seconds": active + sum(waits),
            "estimated_overrun_seconds": max(0.0, active + sum(waits) - duration),
            "episodes": episodes,
        })
    multi_prompt_count = sum(len(s["episodes"]) > 1 for s in planned)
    if len(planned) < min_sessions or multi_prompt_count < min_multi_prompt_sessions:
        candidates = sorted((s for s in planned if len(s["episodes"]) > 1),
                            key=lambda s: s["target_elapsed_seconds"], reverse=True)
        if not candidates:
            raise ValueError("cannot add multi-prompt variants without a source session")
        variant_counts = defaultdict(lambda: 1)
        while len(planned) < min_sessions or multi_prompt_count < min_multi_prompt_sessions:
            source = candidates[(len(planned) - len(sessions)) % len(candidates)]
            variant_counts[source["instance_id"]] += 1
            variant = copy.deepcopy(source)
            variant["session"] = len(planned) + 1
            variant["workload_id"] = (source["instance_id"] + "__variant" +
                                      str(variant_counts[source["instance_id"]]))
            variant["variant_of"] = source["instance_id"]
            waits = [episode["wait_before_seconds"] for episode in variant["episodes"][1:]]
            for episode, wait in zip(variant["episodes"][1:], reversed(waits)):
                episode["wait_before_seconds"] = wait
            planned.append(variant)
            multi_prompt_count += 1
    return {"format": "microvm-multi-prompt-plan-v1",
            "timing_basis": "measured per-session elapsed time in the baseline microVM replay",
            "prompt_source": "synthetic boundaries; Azure gap samples supply relative wait weights",
            "seed": seed, "retain_fraction_requested": fraction,
            "minimum_wait_seconds_requested": min_wait,
            "sessions": planned}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-stage", required=True, type=Path, action="append",
                        help="stage with summary, run-plan, and turn-resource files; repeat for more instances")
    parser.add_argument("--admission-schedule", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--retain-fraction", type=float, default=0.25)
    parser.add_argument("--max-prompts", type=int, default=4)
    parser.add_argument("--min-wait-seconds", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=20261020)
    parser.add_argument("--min-sessions", type=int, default=0,
                        help="add aliased variants of long multi-prompt sources to reach this count")
    parser.add_argument("--min-multi-prompt-sessions", type=int, default=0,
                        help="add variants until this many sessions have two or more prompts")
    parser.add_argument("--multi-only", action="store_true",
                        help="omit source sessions too short for a second prompt")
    args = parser.parse_args()
    if (not 0 < args.retain_fraction <= 1 or args.max_prompts < 1 or
            args.min_wait_seconds < 0 or args.min_sessions < 0 or
            args.min_multi_prompt_sessions < 0):
        parser.error("invalid retention or prompt settings")
    schedule = json.loads(args.admission_schedule.read_text())
    gaps = [float(row["gap_seconds"]) for row in schedule["sessions"]
            if float(row["gap_seconds"]) > 0]
    if not gaps:
        parser.error("admission schedule has no positive gap samples")
    plan = make_plan(args.baseline_stage, args.retain_fraction, args.max_prompts,
                     args.min_wait_seconds, args.seed, gaps, args.min_sessions,
                     args.min_multi_prompt_sessions)
    if args.multi_only:
        plan["sessions"] = [session for session in plan["sessions"]
                            if len(session["episodes"]) > 1]
        for number, session in enumerate(plan["sessions"], 1):
            session["session"] = number
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2) + "\n")
    print(f"wrote {args.output}: {len(plan['sessions'])} sessions, "
          f"{sum(len(s['episodes']) > 1 for s in plan['sessions'])} with multiple prompts")


if __name__ == "__main__":
    main()
