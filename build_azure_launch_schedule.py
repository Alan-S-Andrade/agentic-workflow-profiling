#!/usr/bin/env python3
"""Use Azure user-prompt start-to-start intervals as synthetic YIG VM arrivals.

Read the published daily tar.gz archives without extracting their JSON shards.
Merge session/turn identities across shards and days before deriving intervals.
The YIG trajectories themselves are unchanged. This transfers an empirical
within-conversation distribution to VM admissions; it is not Azure's global
new-session arrival stream.
"""

import argparse
import array
import datetime
import gzip
import gc
import hashlib
import json
import math
import random
import re
import tarfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

try:
    import simdjson  # Optional acceleration: avoid materializing message metadata.
except ImportError:
    simdjson = None

SOURCE = "https://github.com/Azure/AzurePublicDataset/releases/tag/ghcp-coding-agent-2026"


def timestamp(value):
    # Azure writes nine fractional digits; Python 3.10 expects 3 or 6 digits.
    value = re.sub(r"\.(\d+)(?=Z$|[+-]\d{2}:\d{2}$)",
                   lambda match: "." + match[1][:6].ljust(6, "0"), value)
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("source timestamps must include a timezone")
    return parsed.timestamp()


def percentile(sorted_values, percentage):
    index = (len(sorted_values) - 1) * percentage / 100
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (index - lower)


def compact_record(raw, parser):
    if parser is None:
        return json.loads(raw)
    record = parser.parse(raw)
    return {"session_id": record["session_id"], "date": record["date"],
            "turns": [{"turn_id": turn["turn_id"],
                       "llm_calls": [{"timestamp": call["timestamp"], "initiator_type": "user"}
                                     for call in turn["llm_calls"] if call.get("initiator_type") == "user"]}
                      for turn in record["turns"]]}


def read_archive(archive):
    # JSON records are acyclic; avoid repeatedly scanning large metadata trees.
    gc.disable()
    sessions, counts = {}, Counter()
    parser = simdjson.Parser() if simdjson is not None else None
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    source = {"archive": archive.name, "sha256": digest.hexdigest()}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".jsonl.gz"):
                continue
            counts["shards"] += 1
            with gzip.open(tar.extractfile(member), "rb") as stream:
                for line in stream:
                    record = compact_record(line, parser)
                    counts["session_records"] += 1
                    turns = sessions.setdefault(record["session_id"], {})
                    for turn in record["turns"]:
                        counts["turn_records"] += 1
                        calls = [c for c in turn["llm_calls"] if c.get("initiator_type") == "user"]
                        if not calls:
                            counts["turns_without_user_initiator"] += 1
                            continue
                        counts["user_initiated_calls"] += len(calls)
                        try:
                            call = min(calls, key=lambda c: timestamp(c["timestamp"]))
                            start = timestamp(call["timestamp"])
                        except (ValueError, KeyError, TypeError):
                            counts["invalid_user_timestamps"] += 1
                            continue
                        turn_id = turn["turn_id"]
                        info = {"timestamp": call["timestamp"], "start": start,
                                "date": record["date"], "turn_id": turn_id}
                        if turn_id in turns:
                            counts["duplicate_turn_records"] += 1
                            if turns[turn_id]["start"] <= start:
                                continue
                        turns[turn_id] = info
            if counts["shards"] % 20 == 0:
                print(f"{archive.name}: {counts['shards']} shards read", flush=True)
    print(f"read {archive.name}: {counts['session_records']} records", flush=True)
    return sessions, counts, source


def extract_gaps(archives, output, workers):
    sessions, counts, sources = {}, Counter(), []
    with ProcessPoolExecutor(max_workers=min(workers, len(archives))) as pool:
        for partial, partial_counts, source in pool.map(read_archive, archives):
            counts.update(partial_counts)
            sources.append(source)
            for session_id, turns in partial.items():
                existing = sessions.setdefault(session_id, {})
                for turn_id, info in turns.items():
                    if turn_id in existing:
                        counts["duplicate_turn_records"] += 1
                        if existing[turn_id]["start"] <= info["start"]:
                            continue
                    existing[turn_id] = info
    gap_values = array.array("d")
    with output.open("w") as stream:
        for session_id in sorted(sessions):
            turns = sorted(sessions[session_id].values(), key=lambda t: (t["start"], t["turn_id"]))
            if len(turns) > 1:
                counts["sessions_with_multiple_user_turns"] += 1
            for previous, following in zip(turns, turns[1:]):
                gap = following["start"] - previous["start"]
                row = {"session_id": session_id, "previous_turn_id": previous["turn_id"],
                       "next_turn_id": following["turn_id"], "previous_prompt_timestamp": previous["timestamp"],
                       "next_prompt_timestamp": following["timestamp"], "gap_seconds": gap}
                stream.write(json.dumps(row) + "\n")
                gap_values.append(gap)
    counts["unique_sessions"] = len(sessions)
    counts["unique_user_turns"] = sum(len(t) for t in sessions.values())
    counts["prompt_intervals"] = len(gap_values)
    return gap_values, dict(counts), sources


def make_schedule(pool, values, count, seed, scale):
    rng = random.Random(seed)
    indices = [rng.randrange(len(values)) for _ in range(count - 1)]
    wanted, examples = set(indices), {}
    with pool.open() as stream:
        for index, line in enumerate(stream):
            if index in wanted:
                examples[index] = json.loads(line)
            if len(examples) == len(wanted):
                break
    rows = [{"session": 1, "gap_seconds": 0.0, "launch_offset_seconds": 0.0}]
    offset = 0.0
    for number, index in enumerate(indices, 2):
        gap = values[index] * scale
        offset += gap
        rows.append({"session": number, "gap_seconds": gap, "launch_offset_seconds": offset,
                     "azure_gap_index": index, "azure_source": examples[index]})
    return {"format": "microvm-admission-schedule-v1", "source_url": SOURCE,
            "gap_definition": "next user-initiated prompt start minus previous user-initiated prompt start, within the same Azure session",
            "interpretation": "synthetic VM inter-arrivals sampled with replacement; one complete original YIG trace per VM",
            "seed": seed, "gap_scale": scale, "clipping": None,
            "empirical_gap_count": len(values), "sessions": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--azure-root", type=Path, default=Path(".tools/azure-copilot-2026"))
    parser.add_argument("--gap-pool", type=Path, help="reuse previously extracted azure-user-prompt-gaps.jsonl")
    parser.add_argument("--output", type=Path, default=Path("traces/azure-yig-admission"))
    parser.add_argument("--sessions", type=int, default=128)
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--gap-scale", type=float, default=1.0, help="explicit time scaling; 1 preserves observed intervals")
    parser.add_argument("--workers", type=int, default=7, help="parallel archive readers")
    args = parser.parse_args()
    if args.sessions < 1 or args.workers < 1 or not math.isfinite(args.gap_scale) or args.gap_scale <= 0:
        parser.error("sessions and gap scale must be positive")
    args.output.mkdir(parents=True, exist_ok=False)
    if args.gap_pool:
        pool = args.gap_pool.resolve()
        with pool.open() as stream:
            values = array.array("d", (float(json.loads(line)["gap_seconds"]) for line in stream))
        counts, sources = {"prompt_intervals": len(values)}, []
    else:
        archives = sorted(args.azure_root.glob("date.*.tar.gz"))
        if not archives:
            parser.error("no Azure daily archives found; download them from " + SOURCE)
        pool = args.output / "azure-user-prompt-gaps.jsonl"
        values, counts, sources = extract_gaps(archives, pool, args.workers)
    if not values or any(not math.isfinite(v) or v < 0 for v in values):
        parser.error("gap pool must contain finite, nonnegative observed intervals")
    ordered = sorted(values)
    schedule = make_schedule(pool, values, args.sessions, args.seed, args.gap_scale)
    summary = {"source_url": SOURCE, "archives": sources, "counts": counts,
               "gap_definition": schedule["gap_definition"], "gap_pool": str(pool.resolve()),
               "seconds": {"min": ordered[0], "mean": sum(values) / len(values), "max": ordered[-1],
                           **{f"p{p}": percentile(ordered, p) for p in (50, 90, 95, 99)}},
               "scheduled_sessions": args.sessions, "seed": args.seed, "gap_scale": args.gap_scale,
               "last_launch_offset_seconds": schedule["sessions"][-1]["launch_offset_seconds"],
               "source_license": "CC-BY; see Azure DATASET_CARD.md and GitHubCopilotCodingAgentDataset2026.md"}
    (args.output / "launch-schedule.json").write_text(json.dumps(schedule, indent=2) + "\n")
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
