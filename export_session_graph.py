#!/usr/bin/env python3
"""Store a human-session layer over a recorded workflow graph.

The workflow graph contains internal execution and LLM spans.  This exporter
adds the external interaction that initiated them without relabeling each LLM
call as a human turn.
"""

import argparse
import json
import subprocess
from pathlib import Path

from export_graph import dot, load_events, make_graph


def safe_mermaid(graph: dict) -> str:
    """Render Mermaid using synthetic identifiers safe for every span ID."""
    node_ids = {node["id"]: f"n{index:03d}" for index, node in enumerate(graph["nodes"], start=1)}
    lines = ["flowchart LR"]
    for node in graph["nodes"]:
        label = f"{node['node_type']}: {node['name']} ({node['duration_ms']:.3f} ms)"
        if node.get("description"):
            label = f"{node['node_type']}: {node['name']} — {node['description']} ({node['duration_ms']:.3f} ms)"
        lines.append(f'  {node_ids[node["id"]]}["{label.replace(chr(34), chr(39))}"]')
    for edge in graph["edges"]:
        connector = "-->" if edge["recorded"] else "-.->"
        lines.append(f"  {node_ids[edge['source']]} {connector}|{edge['type']}| {node_ids[edge['target']]}")
    return "\n".join(lines)


def make_session_graph(trace_dir: Path) -> dict:
    run_path = trace_dir / "run.json"
    if not run_path.exists():
        raise SystemExit(f"Missing completed-run metadata: {run_path}")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    graph = make_graph(load_events(trace_dir))
    started_at = float(run["started_at"])
    ended_at = float(run["ended_at"])
    session_id = f"session:{trace_dir.name}"
    turn_id = f"turn:{trace_dir.name}:1"
    workflow_nodes = [node for node in graph["nodes"] if node["node_type"] == "workflow"]
    graph["nodes"] = [
        {
            "id": session_id,
            "kind": "session",
            "node_type": "session",
            "name": trace_dir.name,
            "description": "One external conversation session",
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": (ended_at - started_at) * 1000,
            "cpu_ms": None,
            "cpu_utilization_pct": None,
            "peak_rss_bytes": None,
        },
        {
            "id": turn_id,
            "kind": "human_turn",
            "node_type": "human",
            "name": "Human turn 1",
            "description": "External task in run.json#/task",
            "started_at": started_at,
            "ended_at": started_at,
            "duration_ms": 0,
            "cpu_ms": None,
            "cpu_utilization_pct": None,
            "peak_rss_bytes": None,
        },
        *graph["nodes"],
    ]
    graph["edges"] = [
        {"source": session_id, "target": turn_id, "type": "contains", "recorded": True},
        *[
            {"source": turn_id, "target": node["id"], "type": "starts", "recorded": True}
            for node in workflow_nodes
        ],
        *graph["edges"],
    ]
    graph["session"] = {
        "session_id": session_id,
        "human_turn_count": 1,
        "internal_llm_request_count": sum(node["kind"] == "llm" for node in graph["nodes"]),
        "source": "run.json + execution.jsonl + llm.jsonl",
    }
    return graph


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_dir", type=Path)
    parser.add_argument("--render-png", action="store_true")
    args = parser.parse_args()
    graph = make_session_graph(args.trace_dir)
    outputs = {
        "session-graph.json": json.dumps(graph, indent=2) + "\n",
        "session-graph.dot": dot(graph) + "\n",
        "session-graph.mmd": safe_mermaid(graph) + "\n",
        "workflow-graph.mmd": safe_mermaid(make_graph(load_events(args.trace_dir))) + "\n",
    }
    for filename, contents in outputs.items():
        (args.trace_dir / filename).write_text(contents, encoding="utf-8")
    if args.render_png:
        subprocess.run(
            ["dot", "-Tpng", str(args.trace_dir / "session-graph.dot"), "-o", str(args.trace_dir / "session-graph.png")],
            check=True,
        )
    manifest = {
        "timeline": ["timeline.json", "timeline.svg", "timeline.png"],
        "workflow_graph": ["graph.json", "graph.dot", "graph.png", "workflow-graph.mmd"],
        "session_graph": ["session.json", "session.mmd", "session-graph.json", "session-graph.dot", "session-graph.png", "session-graph.mmd"],
        "process_graph": ["process-graph.dot", "process-graph.png"],
        "note": "Use timeline.svg for execution order. Session, workflow, and process graphs retain structural detail.",
    }
    (args.trace_dir / "graph-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(args.trace_dir)


if __name__ == "__main__":
    main()
