#!/usr/bin/env python3
"""Render a three-y-axis 2D view of a colocated Docker replay experiment."""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment", type=Path)
    parser.add_argument("--workflows", type=int, required=True,
                        help="concurrently launched replay sessions")
    args = parser.parse_args()
    samples = [json.loads(line) for line in (args.experiment / "samples.jsonl").read_text().splitlines()]
    if not samples:
        raise SystemExit("no samples found")
    # Keep rendering responsive for long millisecond-sampled experiments.
    stride = max(1, len(samples) // 3000)
    samples = samples[::stride]
    minutes = [item["elapsed_ms"] / 60_000 for item in samples]
    cpu = [item.get("capacity_cpu_percent", item.get("host_cpu_percent")) for item in samples]
    pss_gib = [(item.get("replay_pss_bytes") or 0) / 2**30 for item in samples]
    workflows = [args.workflows] * len(samples)

    width, height = 1800, 900
    left, right, top, bottom = 150, 380, 130, 140
    graph_w, graph_h = width - left - right, height - top - bottom
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font, bold = ImageFont.truetype(font_path, 22), ImageFont.truetype(bold_path, 30)
    colors = {"cpu": "#d97706", "pss": "#2563eb", "sessions": "#15803d"}
    max_minutes = max(minutes) or 1
    cpu_max = max(100, max(value or 0 for value in cpu) * 1.1)
    pss_max = max(1, max(pss_gib) * 1.1)
    session_max = max(1, args.workflows * 1.2)

    def xy(index, value, maximum):
        return (left + minutes[index] / max_minutes * graph_w,
                top + graph_h - value / maximum * graph_h)

    draw.text((left, 38), "OpenHands no-LLM Docker replay colocation", fill="#111827", font=bold)
    draw.line((left, top, left, top + graph_h), fill=colors["cpu"], width=3)
    draw.line((left, top + graph_h, left + graph_w, top + graph_h), fill="#374151", width=2)
    draw.line((left + graph_w, top, left + graph_w, top + graph_h), fill=colors["pss"], width=3)
    draw.line((left + graph_w + 100, top, left + graph_w + 100, top + graph_h), fill=colors["sessions"], width=3)
    for tick in range(6):
        y = top + graph_h - tick / 5 * graph_h
        draw.line((left, y, left + graph_w, y), fill="#e5e7eb", width=1)
        draw.text((20, y - 12), f"{cpu_max * tick / 5:.1f}%", fill=colors["cpu"], font=font)
        draw.text((left + graph_w + 15, y - 12), f"{pss_max * tick / 5:.2f}", fill=colors["pss"], font=font)
        draw.text((left + graph_w + 115, y - 12), f"{session_max * tick / 5:.0f}", fill=colors["sessions"], font=font)
    for tick in range(6):
        x = left + tick / 5 * graph_w
        draw.text((x - 14, top + graph_h + 18), f"{max_minutes * tick / 5:.1f}", fill="#374151", font=font)
    def line(values, maximum, color):
        points = [xy(i, value or 0, maximum) for i, value in enumerate(values)]
        if len(points) > 1:
            draw.line(points, fill=color, width=3)
    line(cpu, cpu_max, colors["cpu"])
    line(pss_gib, pss_max, colors["pss"])
    line(workflows, session_max, colors["sessions"])
    draw.text((left, height - 60), "Elapsed time (minutes)", fill="#374151", font=font)
    draw.text((15, 90), "Capacity-scope CPU utilization", fill=colors["cpu"], font=font)
    draw.text((left + graph_w + 5, 90), "Replay PSS (GiB)", fill=colors["pss"], font=font)
    draw.text((left + graph_w + 100, 90), "Workflows", fill=colors["sessions"], font=font)
    draw.text((left, 88), "orange: CPU   blue: PSS   green: concurrent workflows", fill="#374151", font=font)
    image.save(args.experiment / "colocation-three-axis.png")
    print(args.experiment / "colocation-three-axis.png")


if __name__ == "__main__":
    main()
