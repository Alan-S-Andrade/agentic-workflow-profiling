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
            "cpu_utilization_pct": event.get("cpu_utilization_pct"),
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
    """Render a graph where LLM calls and local work are visually distinct."""
    node_styles = {
        "llm": ("hexagon", "#FDE68A", "#92400E", "LLM API CALL"),
        "tool": ("box", "#BBF7D0", "#166534", "TOOL / I-O"),
        "agent": ("box", "#BFDBFE", "#1D4ED8", "AGENT / CONTROL FLOW"),
        "control": ("ellipse", "#DDD6FE", "#6D28D9", "CONTROL"),
    }

    def memory_label(value: int | None) -> str | None:
        if value is None:
            return None
        return f"{value / (1024 * 1024):.0f} MiB"

    lines = [
        "digraph workflow {",
        "  rankdir=LR;",
        '  graph [fontname="Helvetica", fontsize=20, labelloc="t", label="Workflow trace\\nSolid arrows: recorded parent/dependency · dashed arrows: inferred containment · dotted arrows: temporal order"];',
        '  node [fontname="Helvetica", fontsize=14, penwidth=1.5, style="filled,rounded", margin="0.20,0.14"];',
        '  edge [fontname="Helvetica", fontsize=10, color="#475569"];',
    ]
    for node in graph["nodes"]:
        shape, fillcolor, color, category = node_styles.get(
            node["node_type"], ("box", "#E2E8F0", "#475569", node["node_type"].upper())
        )
        label_lines = [category, node["name"], f"Duration: {node['duration_ms'] / 1000:.2f}s"]
        if node["kind"] == "execution_node":
            if node["cpu_utilization_pct"] is not None:
                label_lines.append(f"CPU: {node['cpu_utilization_pct']:.0f}%")
            peak_rss = memory_label(node["peak_rss_bytes"])
            if peak_rss:
                label_lines.append(f"Peak RSS: {peak_rss}")
        # A newline in a DOT quoted string renders as a line break.  Do not
        # use a literal backslash-n here: Graphviz displays that verbatim.
        label = "\n".join(label_lines)
        lines.append(
            f"  {json.dumps(node['id'])} "
            f"[label={json.dumps(label)},shape={shape},fillcolor={json.dumps(fillcolor)},color={json.dumps(color)}];"
        )
    for edge in graph["edges"]:
        attributes = []
        if edge["type"] == "temporal":
            attributes.extend(("style=dotted", 'color="#94A3B8"', "arrowhead=none"))
        elif not edge["recorded"]:
            attributes.extend(("style=dashed", 'color="#64748B"'))
        elif edge["type"] == "dependency":
            attributes.extend(("penwidth=2", 'color="#2563EB"'))
        else:
            attributes.append('color="#334155"')
        # Parent edges are clear from structure; labels for every one make
        # dense fan-out graphs much harder to read.
        if edge["type"] != "parent":
            attributes.append(f"label={json.dumps(edge['type'])}")
        lines.append(
            f"  {json.dumps(edge['source'])} -> {json.dumps(edge['target'])} [{','.join(attributes)}];"
        )
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


def split_graph(graph: dict, max_nodes: int) -> list[dict]:
    """Split a graph into chronological, independently renderable subgraphs."""
    ordered_nodes = sorted(graph["nodes"], key=lambda node: (node["started_at"], node["ended_at"], node["id"]))
    parts = []
    for index in range(0, len(ordered_nodes), max_nodes):
        nodes = ordered_nodes[index : index + max_nodes]
        node_ids = {node["id"] for node in nodes}
        edges = [
            edge
            for edge in graph["edges"]
            if edge["source"] in node_ids and edge["target"] in node_ids
        ]
        parts.append({"nodes": nodes, "edges": edges})
    return parts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_dir", type=Path)
    parser.add_argument("--format", choices=("json", "dot", "mermaid"), default="json")
    parser.add_argument(
        "--split-nodes",
        type=int,
        metavar="COUNT",
        help="write chronological subgraphs containing at most COUNT nodes each",
    )
    parser.add_argument("--output-dir", type=Path, help="destination directory required with --split-nodes")
    args = parser.parse_args()
    graph = make_graph(load_events(args.trace_dir))
    if args.split_nodes is not None:
        if args.split_nodes < 1:
            parser.error("--split-nodes must be positive")
        if args.output_dir is None:
            parser.error("--output-dir is required with --split-nodes")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        renderers = {"json": lambda value: json.dumps(value, indent=2), "dot": dot, "mermaid": mermaid}
        suffixes = {"json": "json", "dot": "dot", "mermaid": "mmd"}
        parts = split_graph(graph, args.split_nodes)
        manifest = []
        for index, part in enumerate(parts, start=1):
            filename = f"graph-part-{index:03d}.{suffixes[args.format]}"
            (args.output_dir / filename).write_text(renderers[args.format](part) + "\n", encoding="utf-8")
            manifest.append({"file": filename, "node_count": len(part["nodes"]), "edge_count": len(part["edges"])})
        (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(args.output_dir)
        return
    if args.format == "json":
        print(json.dumps(graph, indent=2))
    elif args.format == "dot":
        print(dot(graph))
    else:
        print(mermaid(graph))


if __name__ == "__main__":
    main()
