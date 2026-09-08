#!/usr/bin/env python3
"""Summarize execution and LLM spans into a wall-clock critical path.

The calculation uses non-overlapping spans: a candidate step can follow a
previous step only when the previous step has finished.  Container execution
nodes are omitted when they contain child execution nodes, preventing an
agent-loop wrapper from hiding the actual steps inside it.
"""

import argparse
import json
from pathlib import Path


def read_jsonl(path: Path, kind: str) -> list[dict]:
    if not path.exists():
        return []
    spans = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("kind") == kind and event.get("event", "end") == "end":
            start = event.get("started_at")
            duration = event.get("duration_ms")
            if start is not None and duration is not None:
                spans.append({
                    "kind": kind,
                    "name": event.get("name", event.get("path", "llm")),
                    "node_type": event.get("node_type", "llm"),
                    "started_at": float(start),
                    "ended_at": float(start) + float(duration) / 1000,
                    "duration_ms": float(duration),
                    "cpu_ms": event.get("cpu_ms"),
                    "cpu_utilization_pct": event.get("cpu_utilization_pct"),
                    "rss_delta_bytes": event.get("rss_delta_bytes"),
                    "peak_rss_bytes": event.get("peak_rss_bytes"),
                    "process_peak_rss_bytes": event.get("process_peak_rss_bytes"),
                    "children_peak_rss_bytes": event.get("children_peak_rss_bytes"),
                    "python_net_allocated_bytes": event.get("python_net_allocated_bytes"),
                    "span_id": event.get("span_id"),
                })
    return spans


def leaf_execution_spans(spans: list[dict]) -> list[dict]:
    execution = [span for span in spans if span["kind"] == "execution_node"]
    leaves = []
    for span in execution:
        contains_child = any(
            child is not span
            and child["started_at"] >= span["started_at"]
            and child["ended_at"] <= span["ended_at"]
            and (child["started_at"] > span["started_at"] or child["ended_at"] < span["ended_at"])
            for child in execution
        )
        if not contains_child:
            leaves.append(span)
    return leaves


def critical_path(spans: list[dict]) -> list[dict]:
    candidates = leaf_execution_spans(spans) + [s for s in spans if s["kind"] == "llm"]
    candidates.sort(key=lambda span: (span["ended_at"], span["started_at"]))
    best: list[float] = []
    previous: list[int | None] = []
    for index, span in enumerate(candidates):
        prior = [j for j in range(index) if candidates[j]["ended_at"] <= span["started_at"]]
        prior_index = max(prior, key=lambda j: best[j], default=None)
        best.append(span["duration_ms"] + (best[prior_index] if prior_index is not None else 0))
        previous.append(prior_index)
    if not candidates:
        return []
    index = max(range(len(candidates)), key=best.__getitem__)
    result = []
    while index is not None:
        result.append(candidates[index])
        index = previous[index]
    return list(reversed(result))


def main() -> None:
    parser = argparse.ArgumentParser(description="Report a candidate critical path for a profile trace")
    parser.add_argument("trace_dir", type=Path, help="A directory produced by profile-run.sh")
    args = parser.parse_args()
    spans = read_jsonl(args.trace_dir / "execution.jsonl", "execution_node")
    spans += read_jsonl(args.trace_dir / "llm.jsonl", "llm")
    path = critical_path(spans)
    report = {
        "critical_path_duration_ms": round(sum(span["duration_ms"] for span in path), 3),
        "steps": [
            {
                "kind": span["kind"],
                "node_type": span["node_type"],
                "name": span["name"],
                "duration_ms": round(span["duration_ms"], 3),
                "cpu_ms": span["cpu_ms"],
                "cpu_utilization_pct": span.get("cpu_utilization_pct"),
                "rss_delta_bytes": span.get("rss_delta_bytes"),
                "peak_rss_bytes": span.get("peak_rss_bytes"),
                "process_peak_rss_bytes": span.get("process_peak_rss_bytes"),
                "children_peak_rss_bytes": span.get("children_peak_rss_bytes"),
                "python_net_allocated_bytes": span.get("python_net_allocated_bytes"),
                "started_at": span["started_at"],
            }
            for span in path
        ],
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
