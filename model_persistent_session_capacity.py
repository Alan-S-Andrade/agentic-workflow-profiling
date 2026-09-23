#!/usr/bin/env python3
"""Plot memory capacity for one persistent OpenHands sandbox per session.

The calibration is deliberately PSS-based: aggregate PSS divided by the number
of concurrently resident workflows.  Unlike RSS, PSS apportions shared pages
across containers, so the model does not count common image/library mappings
once per session.
"""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


GIB = 2**30
MIB = 2**20


def nonzero_pss_peak(path: Path) -> int:
    values = []
    for line in path.read_text().splitlines():
        value = json.loads(line).get("replay_pss_bytes")
        if value:
            values.append(value)
    if not values:
        raise SystemExit(f"no PSS samples in {path}")
    return max(values)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment", type=Path,
                        help="completed colocation directory containing samples.jsonl and summary.json")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--host-gib", type=float, default=264011428 * 1024 / GIB)
    parser.add_argument("--reserve-percent", type=float, default=15,
                        help="capacity reserved for OS, Docker, and other work")
    args = parser.parse_args()

    summary = json.loads((args.experiment / "summary.json").read_text())
    sessions = summary["concurrency"]
    aggregate = nonzero_pss_peak(args.experiment / "samples.jsonl")
    pss_per_session = aggregate / sessions
    usable_gib = args.host_gib * (1 - args.reserve_percent / 100)
    nominal_capacity = int(usable_gib * GIB // pss_per_session)
    scenarios = [(1, "measured PSS", "#2563eb"),
                 (2, "2x measured PSS", "#d97706"),
                 (4, "4x measured PSS", "#be123c")]
    xmax = max(100, int(nominal_capacity * 1.15))

    width, height = 1800, 1000
    left, right, top, bottom = 165, 120, 245, 150
    graph_w, graph_h = width - left - right, height - top - bottom
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font = ImageFont.truetype(font_path, 24)
    small = ImageFont.truetype(font_path, 20)
    bold = ImageFont.truetype(bold_path, 34)
    y_max = args.host_gib * 1.10

    def x(n): return left + n / xmax * graph_w
    def y(gib): return top + graph_h - gib / y_max * graph_h

    draw.text((left, 38), "Persistent OpenHands sandbox capacity model", fill="#111827", font=bold)
    subtitle = (f"Calibration: {aggregate / GIB:.3f} GiB aggregate peak PSS / {sessions} sessions "
                f"= {pss_per_session / MIB:.2f} MiB PSS per session")
    draw.text((left, 88), subtitle, fill="#374151", font=font)
    draw.text((left, 124), "One long-lived tool-execution container per session; common mapped pages are PSS-apportioned.",
              fill="#374151", font=small)
    for index, (multiplier, label, color) in enumerate(scenarios):
        cap = int(usable_gib * GIB // (pss_per_session * multiplier))
        legend_y = 150 + index * 27
        draw.line((left, legend_y + 12, left + 42, legend_y + 12), fill=color, width=4)
        text = f"{label}: {cap:,} sessions at planning limit"
        draw.text((left + 52, legend_y), text, fill=color, font=small)

    # Capacity bands and axes.
    draw.rectangle((left, y(args.host_gib), left + graph_w, y(usable_gib)), fill="#fee2e2")
    draw.rectangle((left, y(usable_gib), left + graph_w, top + graph_h), fill="#f0fdf4")
    for tick in range(7):
        gib = args.host_gib * tick / 6
        yy = y(gib)
        draw.line((left, yy, left + graph_w, yy), fill="#d1d5db", width=1)
        draw.text((35, yy - 12), f"{gib:.0f} GiB", fill="#374151", font=small)
    for tick in range(7):
        n = xmax * tick / 6
        xx = x(n)
        draw.line((xx, top, xx, top + graph_h), fill="#e5e7eb", width=1)
        draw.text((xx - 25, top + graph_h + 20), f"{n / 1000:.1f}k", fill="#374151", font=small)
    draw.line((left, top, left, top + graph_h), fill="#374151", width=2)
    draw.line((left, top + graph_h, left + graph_w, top + graph_h), fill="#374151", width=2)

    draw.line((left, y(args.host_gib), left + graph_w, y(args.host_gib)), fill="#991b1b", width=3)
    draw.text((left + 15, y(args.host_gib) + 8), f"host physical memory: {args.host_gib:.1f} GiB", fill="#991b1b", font=small)
    draw.line((left, y(usable_gib), left + graph_w, y(usable_gib)), fill="#15803d", width=3)
    draw.text((left + 15, y(usable_gib) + 8), f"planning limit ({100 - args.reserve_percent:.0f}%): {usable_gib:.1f} GiB", fill="#166534", font=small)

    for multiplier, label, color in scenarios:
        footprint = pss_per_session * multiplier
        draw.line((x(0), y(0), x(xmax), y(xmax * footprint / GIB)), fill=color, width=4)
        cap = int(usable_gib * GIB // footprint)
        xx = x(cap)
        draw.line((xx, y(0), xx, y(usable_gib)), fill=color, width=2)

    observed_y = aggregate / GIB
    draw.ellipse((x(sessions) - 7, y(observed_y) - 7, x(sessions) + 7, y(observed_y) + 7), fill="#111827")
    draw.text((x(sessions) + 14, y(observed_y) - 30), f"observed: {sessions} sessions, {observed_y:.3f} GiB", fill="#111827", font=small)
    draw.text((left, height - 62), "Concurrent resident sessions (one persistent sandbox each)", fill="#374151", font=font)
    draw.text((25, 160), "Aggregate container PSS", fill="#374151", font=font)

    output = args.output or args.experiment / "persistent-session-capacity.png"
    image.save(output)
    report = {
        "experiment": str(args.experiment), "sessions_measured": sessions,
        "aggregate_peak_pss_bytes": aggregate, "pss_per_session_bytes": pss_per_session,
        "pss_per_session_mib": pss_per_session / MIB, "host_gib": args.host_gib,
        "reserve_percent": args.reserve_percent, "usable_gib": usable_gib,
        "session_capacity": {f"{m}x_measured_pss": int(usable_gib * GIB // (pss_per_session * m)) for m, _, _ in scenarios},
    }
    output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    print(output)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
