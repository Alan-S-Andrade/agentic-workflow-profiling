#!/usr/bin/env python3
"""Export the recorded workflow as JSON, Graphviz DOT, or Mermaid.

Explicit parent/dependency edges are marked ``recorded``.  Edges inferred
from span containment or non-overlapping timestamps are marked ``inferred``
so downstream analysis can filter them out when exact causal instrumentation
is available.
"""

import argparse
import json
from pathlib import Path


def load_events(trace_dir: Path) -> list[dict]:
    events = []
    for filename in ("execution.jsonl", "llm.jsonl"):
        path = trace_dir / filename
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event", "end") == "end":
                events.append(event)
    return events


def make_graph(events: list[dict]) -> dict:
    nodes = []
    by_id = {}
    for index, event in enumerate(events):
        node_id = event.get("span_id") or event.get("request_id") or f"node-{index}"
        if node_id in by_id:
            continue
        duration = float(event.get("duration_ms", 0))
        node = {
            "id": node_id,
            "kind": event.get("kind", "unknown"),
            "node_type": event.get("node_type", "llm"),
            "name": event.get("name", event.get("path", "llm")),
            "started_at": event.get("started_at"),
            "ended_at": float(event.get("started_at", 0)) + duration / 1000,
            "duration_ms": duration,
            "cpu_ms": event.get("cpu_ms"),
            "peak_rss_bytes": event.get("peak_rss_bytes"),
        }
        nodes.append(node)
        by_id[node_id] = node

    edges = []
    edge_keys = set()

    def add_edge(source, target, edge_type, recorded):
        if source == target or source not in by_id or target not in by_id:
            return
        key = (source, target, edge_type)
        if key not in edge_keys:
            edge_keys.add(key)
            edges.append({"source": source, "target": target, "type": edge_type, "recorded": recorded})

    for event in events:
        target = event.get("span_id") or event.get("request_id")
        parent = event.get("parent_span_id")
        add_edge(parent, target, "parent", True)
        dependencies = event.get("depends_on", [])
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        for source in dependencies:
            add_edge(source, target, "dependency", True)

    # LLM requests are recorded by a separate proxy process. If their timing
    # lies inside an execution span, retain that useful but inferred relation.
    for outer in nodes:
        if outer["kind"] != "execution_node":
            continue
        for inner in nodes:
            if inner["kind"] == "llm" and outer["started_at"] <= inner["started_at"] and inner["ended_at"] <= outer["ended_at"]:
                add_edge(outer["id"], inner["id"], "contains", False)

    # Add ordering edges only for adjacent, non-overlapping nodes. These are
    # scheduling hints, not claims about data dependency.
    ordered = sorted(nodes, key=lambda node: (node["ended_at"], node["started_at"]))
    for previous, current in zip(ordered, ordered[1:]):
        if previous["ended_at"] <= current["started_at"]:
            add_edge(previous["id"], current["id"], "temporal", False)

    return {"nodes": nodes, "edges": edges}


def dot(graph: dict) -> str:
    lines = ["digraph workflow {", "  rankdir=LR;"]
    for node in graph["nodes"]:
        label = f"{node['node_type']}: {node['name']}\\n{node['duration_ms']:.3f} ms"
        lines.append(f"  {json.dumps(node['id'])} [label={json.dumps(label)}];")
    styles = {"recorded": "", "inferred": " [style=dashed,color=gray]"}
    for edge in graph["edges"]:
        label = edge["type"]
        lines.append(f"  {json.dumps(edge['source'])} -> {json.dumps(edge['target'])} [label={json.dumps(label)}{styles['recorded' if edge['recorded'] else 'inferred']}];")
    lines.append("}")
    return "\n".join(lines)


def mermaid(graph: dict) -> str:
    lines = ["flowchart LR"]
    for node in graph["nodes"]:
        label = f"{node['node_type']}: {node['name']} ({node['duration_ms']:.3f} ms)".replace('"', "'")
        lines.append(f"  {node['id']}[\"{label}\"]")
    for edge in graph["edges"]:
        connector = "-->" if edge["recorded"] else "-.->"
        lines.append(f"  {edge['source']} {connector}|{edge['type']}| {edge['target']}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_dir", type=Path)
    parser.add_argument("--format", choices=("json", "dot", "mermaid"), default="json")
    args = parser.parse_args()
    graph = make_graph(load_events(args.trace_dir))
    if args.format == "json":
        print(json.dumps(graph, indent=2))
    elif args.format == "dot":
        print(dot(graph))
    else:
        print(mermaid(graph))


if __name__ == "__main__":
    main()
