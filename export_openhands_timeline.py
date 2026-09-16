#!/usr/bin/env python3
"""Render a multi-turn OpenHands timeline with sampled host resource usage."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_dir", type=Path)
    args = parser.parse_args()
    turns = [json.loads(line) for line in (args.trace_dir / "turns.jsonl").read_text().splitlines()]
    process_events = [json.loads(line) for line in (args.trace_dir / "process-samples.jsonl").read_text().splitlines()]
    commands = {(event["pid"], event["start_ticks"]): event.get("cmdline", "") for event in process_events if event.get("event") == "start"}
    samples = [event for event in process_events if event.get("event") == "sample"]

    def role(sample: dict) -> str | None:
        command = commands.get((sample["pid"], sample["start_ticks"]), "")
        if "trace_proxy.py" in command:
            return "proxy"
        if "static-server.mjs" in command or "ingress.mjs" in command:
            return "frontend"
        if "agent-server" in command and "uvx" not in command:
            return "agent_server"
        if command.startswith(("bash ", "/bin/bash ", "sh ", "/bin/sh ")) and "trace_dir=" not in command:
            return "tools"
        return None
    llm_path = args.trace_dir / "llm.jsonl"
    if not llm_path.exists():  # Compatibility with the first run, which reused a proxy trace.
        llm_path = args.trace_dir.parent / "openhands-multiturn-20260915T153000Z" / "llm.jsonl"
    llms = [json.loads(line) for line in llm_path.read_text().splitlines()]
    tool_profile_path = args.trace_dir / "tool-spans.jsonl"
    tool_spans = [json.loads(line) for line in tool_profile_path.read_text().splitlines()] if tool_profile_path.exists() else []
    stats = []
    for turn in turns:
        start, end = turn["started_at"], turn["ended_at"]
        by_time = defaultdict(list)
        by_process = defaultdict(list)
        for sample in samples:
            if start <= sample["observed_at"] <= end:
                by_time[sample["observed_at"]].append(sample)
                by_process[(sample["pid"], sample["start_ticks"])].append(sample["cpu_ms"])
        pss_totals = [
            sum(item.get("pss_bytes") or 0 for item in group)
            for group in by_time.values()
            if any(item.get("pss_bytes") is not None for item in group)
        ]
        pss = max(pss_totals, default=None)
        cpu = sum(max(values) - min(values) for values in by_process.values() if values)
        lanes = {}
        for lane in ("agent_server", "tools", "frontend", "proxy"):
            lane_samples = [sample for sample in samples if start <= sample["observed_at"] <= end and role(sample) == lane]
            lane_processes = defaultdict(list)
            for sample in lane_samples:
                lane_processes[(sample["pid"], sample["start_ticks"])].append(sample["cpu_ms"])
            lanes[lane] = {
                "observed": bool(lane_samples),
                "cpu_ms": sum(max(values) - min(values) for values in lane_processes.values()),
                "peak_pss_bytes": max((sample.get("pss_bytes") or 0 for sample in lane_samples), default=None),
                "core_numbers": sorted({sample["cpu_number"] for sample in lane_samples if sample.get("cpu_number") is not None}),
                "cores_observed": any(sample.get("cpu_number") is not None for sample in lane_samples),
            }
        matched_tool_spans = [
            span for span in tool_spans
            if start <= span.get("started_at", 0) <= end
        ]
        if matched_tool_spans:
            span_cores = sorted({
                profile["core_number"]
                for span in matched_tool_spans
                for profile in (span.get("start"), span.get("end"))
                if profile and profile.get("core_number") is not None
            })
            span_pss = [
                profile["pss_bytes"]
                for span in matched_tool_spans
                for profile in (span.get("start"), span.get("end"))
                if profile and profile.get("pss_bytes") is not None
            ]
            lanes["tools"].update({
                "observed": True,
                "cpu_ms": sum(span.get("cpu_ms") or 0 for span in matched_tool_spans),
                "peak_pss_bytes": max(span_pss, default=None),
                "core_numbers": span_cores,
                "cores_observed": bool(span_cores),
                "span_count": len(matched_tool_spans),
                "resource_source": "terminal-child rusage; core/PSS start snapshot",
            })
        tool_actions = [
            event for event in turn.get("events", [])
            if (event.get("action") or {}).get("kind") == "TerminalAction"
        ]
        lanes["tools"]["action_count"] = len(tool_actions)
        lanes["tools"]["observed_actions"] = bool(tool_actions)
        llm_count = sum(start <= item["started_at"] <= end for item in llms)
        hardware_paths = sorted(args.trace_dir.glob(f"hardware-turn-{turn['turn_id']:02d}-attempt-*.json"))
        if not hardware_paths:
            legacy = args.trace_dir / f"hardware-turn-{turn['turn_id']:02d}.json"
            hardware_paths = [legacy] if legacy.exists() else []
        hardware_records = [json.loads(path.read_text()) for path in hardware_paths]
        counter_totals = defaultdict(int)
        for hardware in hardware_records:
            for process in hardware.get("processes", []):
                for event, value in process.get("counters", {}).items():
                    if isinstance(value, int):
                        counter_totals[event] += value
        cycles, instructions = counter_totals.get("cycles"), counter_totals.get("instructions")
        hardware_summary = {
            "observed": bool(hardware_records),
            "cycles": cycles,
            "instructions": instructions,
            "cache_misses": counter_totals.get("cache-misses"),
            "dtlb_misses": counter_totals.get("dTLB-load-misses") or counter_totals.get("l1d_tlb_refill"),
            "itlb_misses": counter_totals.get("iTLB-load-misses") or counter_totals.get("l1i_tlb_refill"),
            "l2_refills": counter_totals.get("l2d_cache_refill"),
            "ipc": instructions / cycles if cycles else None,
            "llc": "unavailable" if hardware_records and not any(record.get("llc_events") for record in hardware_records) else "measured",
            "memory_bandwidth": "unavailable" if hardware_records and not any(record.get("memory_bandwidth_events") for record in hardware_records) else "measured",
        }
        stats.append({"turn_id": turn["turn_id"], "started_at": start, "ended_at": end, "duration_ms": (end-start)*1000, "cpu_ms": cpu, "peak_pss_bytes": pss, "llm_request_count": llm_count, "event_count": turn["event_count"], "input": turn["input"], "lanes": lanes, "hardware": hardware_summary})
    (args.trace_dir / "timeline-stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    (args.trace_dir / "process-lanes.json").write_text(json.dumps([{ "turn_id": item["turn_id"], "lanes": item["lanes"] } for item in stats], indent=2) + "\n")
    width, height = 2500, 300 + len(stats) * 540
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font, bold = ImageFont.truetype(font_path, 24), ImageFont.truetype(bold_path, 30)
    small = ImageFont.truetype(font_path, 20)
    draw.text((60, 42), "OpenHands multi-turn session timeline", fill="#1f2937", font=bold)
    draw.text((60, 90), f"One conversation · {len(stats)} human turns · model: gpt-5.6-luna", fill="#475569", font=font)
    draw.text((60, 130), "CPU is sampled at 10 ms; PSS is sampled at 250 ms. Hardware counters attach to observed local PIDs.", fill="#475569", font=small)
    y = 190
    for stat in stats:
        draw.line((104, y, 104, y + 315), fill="#94a3b8", width=5)
        draw.ellipse((88, y + 12, 120, y + 44), fill="#0369a1")
        draw.rounded_rectangle((150, y, 2420, y + 470), radius=14, fill="#f8fafc", outline="#94a3b8", width=2)
        draw.text((185, y + 24), f"Human turn {stat['turn_id']}  ·  {stat['duration_ms']/1000:.2f}s", fill="#1f2937", font=bold)
        draw.rounded_rectangle((185, y + 82, 715, y + 144), radius=9, fill="#fbbf24", outline="#92400e")
        draw.text((210, y + 101), f"LLM calls: {stat['llm_request_count']}", fill="#1f2937", font=font)
        draw.rounded_rectangle((755, y + 82, 1320, y + 144), radius=9, fill="#bbf7d0", outline="#166534")
        draw.text((780, y + 101), f"Process CPU: {stat['cpu_ms']:.0f} ms", fill="#1f2937", font=font)
        draw.rounded_rectangle((1360, y + 82, 2100, y + 144), radius=9, fill="#e0f2fe", outline="#0369a1")
        pss_label = f"Peak PSS: {stat['peak_pss_bytes']/1024/1024:.1f} MiB" if stat["peak_pss_bytes"] is not None else "Peak PSS: not sampled"
        draw.text((1385, y + 101), pss_label, fill="#1f2937", font=font)
        lane_y = y + 168
        for index, (name, lane) in enumerate(stat["lanes"].items()):
            x = 185 + (index % 2) * 950
            current_y = lane_y + (index // 2) * 48
            pss_text = f"PSS {lane['peak_pss_bytes']/1024/1024:.1f} MiB" if lane["peak_pss_bytes"] is not None else "PSS not sampled"
            if name == "tools" and lane.get("resource_source"):
                cores = ",".join(map(str, lane["core_numbers"])) if lane["cores_observed"] else "unavailable"
                text = f"tools: {lane['span_count']} terminal span(s) · CPU {lane['cpu_ms']:.0f} ms · {pss_text} · start cores {cores}"
            elif name == "tools" and lane.get("observed_actions") and not lane["observed"]:
                text = f"tools: {lane['action_count']} terminal action(s) · process resources not observed"
            else:
                cores = ",".join(map(str, lane["core_numbers"])) if lane["cores_observed"] else "unavailable"
                text = f"{name.replace('_', ' ')}: " + (f"CPU {lane['cpu_ms']:.0f} ms · {pss_text} · cores {cores}" if lane["observed"] else "not observed")
            draw.text((x, current_y), text, fill="#475569", font=small)
        hardware = stat["hardware"]
        if hardware["observed"]:
            ipc = f"{hardware['ipc']:.2f}" if hardware["ipc"] is not None else "not counted"
            counter_text = f"Perf: cycles {hardware['cycles'] or 0:,} · instructions {hardware['instructions'] or 0:,} · IPC {ipc} · cache misses {hardware['cache_misses'] or 0:,}"
            availability_text = f"LLC misses: {hardware['llc']} · memory bandwidth: {hardware['memory_bandwidth']}"
        else:
            counter_text, availability_text = "Perf: not collected", "LLC misses: unavailable · memory bandwidth: unavailable"
        draw.text((185, y + 270), counter_text, fill="#7c2d12", font=small)
        draw.text((185, y + 300), availability_text, fill="#7c2d12", font=small)
        draw.text((185, y + 332), f"TLB misses: D {hardware['dtlb_misses'] or 0:,} · I {hardware['itlb_misses'] or 0:,} · L2 refills: {hardware['l2_refills'] or 0:,}", fill="#7c2d12", font=small)
        draw.text((185, y + 362), stat["input"], fill="#334155", font=small, spacing=6)
        draw.text((185, y + 422), f"Conversation events recorded: {stat['event_count']}", fill="#475569", font=small)
        y += 520
    image.save(args.trace_dir / "timeline.png")
    print(args.trace_dir)


if __name__ == "__main__":
    main()
