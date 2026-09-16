#!/usr/bin/env python3
"""Render a trace as a time-faithful SVG timeline.

Unlike a dependency graph, horizontal position here always means wall-clock
time.  It deliberately draws containment as a lane rather than fan-out edges.
"""

import argparse
import html
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from export_graph import description_for_event

COLORS = {"workflow": "#94a3b8", "llm": "#fbbf24", "tool": "#86efac"}
LANES = (("workflow", "Workflow envelope"), ("llm", "LLM requests"), ("tool", "Tool actions"))


def events_for(trace_dir: Path) -> tuple[dict, list[dict]]:
    run = json.loads((trace_dir / "run.json").read_text(encoding="utf-8"))
    events = []
    for filename in ("execution.jsonl", "llm.jsonl"):
        for line in (trace_dir / filename).read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event", "end") != "end":
                continue
            category = "llm" if event.get("kind") == "llm" else event.get("node_type")
            if category not in COLORS:
                continue
            start = float(event["started_at"])
            end = start + float(event["duration_ms"]) / 1000
            events.append(
                {
                    "id": event.get("span_id") or event.get("request_id"),
                    "category": category,
                    "start": start,
                    "end": end,
                    "duration_ms": event["duration_ms"],
                    "status": event.get("status"),
                    "name": event.get("name", event.get("path", "LLM request")),
                    "command": event.get("command"),
                    "description": description_for_event(event),
                }
            )
    return run, sorted(events, key=lambda event: (event["start"], event["end"]))


def render_svg(run: dict, events: list[dict]) -> str:
    width, margin_left, margin_right = 2200, 180, 60
    plot_width = width - margin_left - margin_right
    row_height, lane_gap, top = 36, 44, 64
    duration = float(run["ended_at"]) - float(run["started_at"])
    height = top + len(LANES) * (row_height + lane_gap) + 70

    def x(timestamp: float) -> float:
        return margin_left + (timestamp - float(run["started_at"])) / duration * plot_width

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,sans-serif;fill:#1f2937}.axis{font-size:13px}.label{font-size:15px;font-weight:bold}.bar{stroke-width:1.2}.event{font-size:11px}</style>",
        f'<rect width="100%" height="100%" fill="white"/><text x="{margin_left}" y="28" class="label">SWE-agent timeline — 1 human turn, {duration:.1f}s</text>',
    ]
    for second in range(0, int(duration) + 1, 20):
        tick_x = x(float(run["started_at"]) + second)
        parts.append(f'<line x1="{tick_x:.1f}" y1="{top - 20}" x2="{tick_x:.1f}" y2="{height - 34}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{tick_x:.1f}" y="{top - 27}" class="axis" text-anchor="middle">{second}s</text>')
    for lane_index, (category, title) in enumerate(LANES):
        y = top + lane_index * (row_height + lane_gap)
        parts.append(f'<text x="{margin_left - 16}" y="{y + 23}" class="label" text-anchor="end">{title}</text>')
        parts.append(f'<rect x="{margin_left}" y="{y}" width="{plot_width}" height="{row_height}" rx="4" fill="#f8fafc" stroke="#cbd5e1"/>')
        lane_events = [event for event in events if event["category"] == category]
        for index, event in enumerate(lane_events, start=1):
            start_x, end_x = x(event["start"]), x(event["end"])
            bar_width = max(2, end_x - start_x)
            color = "#fca5a5" if event["status"] == "error" else COLORS[category]
            event_label = category[0].upper() + f"{index:02d}"
            title_text = event["name"]
            if event["command"]:
                title_text += ": " + event["command"]
            title_text += f" ({event['duration_ms']:.3f} ms, {event['status']})"
            parts.append(
                f'<g><title>{html.escape(title_text)}</title><rect class="bar" x="{start_x:.1f}" y="{y + 5}" width="{bar_width:.1f}" height="{row_height - 10}" rx="3" fill="{color}" stroke="#334155"/>'
                f'<text x="{start_x + bar_width / 2:.1f}" y="{y + 23}" class="event" text-anchor="middle">{event_label}</text></g>'
            )
    parts.append('<text x="180" y="' + str(height - 12) + '" class="axis">Yellow = LLM inference · green = successful tool action · red = failed/recovered tool action · hover a bar for its recorded operation</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def render_png(run: dict, events: list[dict], output: Path) -> None:
    """Render every agent cycle as readable, chronologically ordered cards."""
    import textwrap

    llms = [event for event in events if event["category"] == "llm"]
    groups = [{"llm": None, "tools": []}] + [{"llm": llm, "tools": []} for llm in llms]
    for tool in (event for event in events if event["category"] == "tool"):
        preceding = [index for index, llm in enumerate(llms, start=1) if llm["end"] <= tool["start"]]
        groups[preceding[-1] if preceding else 0]["tools"].append(tool)

    def group_height(group: dict) -> int:
        # Header plus one model card and two-column tool cards.
        return 42 + (54 if group["llm"] else 0) + max(1, (len(group["tools"]) + 1) // 2) * 52 + 18

    width, margin, top = 3000, 48, 120
    heights = [group_height(group) for group in groups]
    height = top + sum(heights) + 64
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 17)
        bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 19)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except OSError:
        font = bold = small = ImageFont.load_default()

    started = float(run["started_at"])
    draw.text((margin, 26), f"SWE-agent agent-cycle timeline — 1 human turn, {float(run['ended_at']) - started:.1f}s", fill="#1f2937", font=bold)
    draw.text((margin, 58), "Rows are in observed start-time order. Each yellow card is an inference; its following green/red cards are the shell actions before the next inference.", fill="#475569", font=font)
    draw.text((margin, 82), "The offset and duration are recorded wall-clock values; this layout is intentionally not a parallelism graph.", fill="#475569", font=font)

    def offset(event: dict) -> str:
        return f"+{event['start'] - started:.1f}s · {event['duration_ms'] / 1000:.2f}s"

    def action_text(event: dict) -> str:
        text = event.get("description") or event["name"]
        if text == "Run a shell command in the target repository" and event.get("command"):
            first = next((line.strip() for line in event["command"].splitlines() if line.strip()), "shell command")
            text = first
        return textwrap.shorten(text, width=78, placeholder="…")

    y = top
    tool_number = 0
    for group_index, (group, row_height) in enumerate(zip(groups, heights)):
        draw.line((180, y, 180, y + row_height), fill="#cbd5e1", width=3)
        if group["llm"] is None:
            title = f"Setup before first inference — {len(group['tools'])} shell actions"
            group_offset = min((tool["start"] for tool in group["tools"]), default=started) - started
        else:
            title = f"Cycle {group_index:02d} — model inference and subsequent actions"
            group_offset = group["llm"]["start"] - started
        draw.ellipse((170, y + 10, 190, y + 30), fill="#334155")
        draw.text((214, y + 8), f"{title}  (starts +{group_offset:.1f}s)", fill="#1f2937", font=bold)
        card_y = y + 40
        if group["llm"] is not None:
            llm = group["llm"]
            draw.rounded_rectangle((214, card_y, 1170, card_y + 42), radius=7, fill="#fbbf24", outline="#92400e", width=2)
            draw.text((232, card_y + 11), f"L{group_index:02d}  ·  LLM inference  ·  {offset(llm)}", fill="#1f2937", font=font)
            card_y += 50
        for local_index, tool in enumerate(group["tools"]):
            tool_number += 1
            column, row = local_index % 2, local_index // 2
            x = 214 + column * 1392
            tool_y = card_y + row * 52
            color = "#fca5a5" if tool["status"] == "error" else "#86efac"
            draw.rounded_rectangle((x, tool_y, x + 1350, tool_y + 42), radius=7, fill=color, outline="#166534", width=2)
            draw.text((x + 16, tool_y + 5), f"T{tool_number:02d}  ·  {offset(tool)}", fill="#1f2937", font=small)
            draw.text((x + 16, tool_y + 22), action_text(tool), fill="#1f2937", font=small)
        y += row_height
    draw.text((margin, height - 34), "Yellow = LLM inference · green = successful tool action · red = failed/recovered tool action", fill="#1f2937", font=font)
    image.save(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_dir", type=Path)
    args = parser.parse_args()
    run, events = events_for(args.trace_dir)
    payload = {"run": run, "events": events, "semantics": "x-axis is wall-clock time; lanes are categories, not dependency edges."}
    (args.trace_dir / "timeline.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (args.trace_dir / "timeline.svg").write_text(render_svg(run, events) + "\n", encoding="utf-8")
    render_png(run, events, args.trace_dir / "timeline.png")
    print(args.trace_dir)


if __name__ == "__main__":
    main()
