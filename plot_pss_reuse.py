#!/usr/bin/env python3
"""Summarize and plot microVM PSS reuse experiments."""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

import matplotlib.pyplot as plt


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def mib(kb: float) -> float:
    return kb / 1024.0


FILE_CATEGORY_LABELS = {
    "compiler_toolchain": "Compiler toolchain",
    "libraries": "Shared libraries",
    "workspace_files": "Workspace files",
    "interpreter_or_tool": "Shell / Go / Git tools",
    "interpreter_python": "Python interpreter",
}


def intra_series(rows: list[dict]):
    rows = [x for x in rows if x.get("experiment") == "intra"]
    rows.sort(key=lambda x: x["turn"])
    result = collections.defaultdict(list)
    turns = []
    for row in rows:
        turns.append(row["turn"])
        host = row["host"]
        guest = row.get("guest", {})
        categories = host.get("categories", {})
        meminfo = guest.get("meminfo_kb", {})
        result["VMM + guest total PSS"].append(mib(host.get("totals", {}).get("Pss_kb", 0)))
        result["Guest RAM mapping PSS"].append(mib(categories.get("guest_ram_anonymous", {}).get("Pss_kb", 0)))
        result["Guest page cache"].append(mib(meminfo.get("Cached", 0)))
        result["Guest slab"].append(mib(meminfo.get("Slab", 0)))
        result["Guest process PSS"].append(mib(sum(v.get("Pss", 0) for v in guest.get("process_mapping_categories_kb", {}).values())))
    return turns, result


def cross_final(rows: list[dict]) -> dict:
    finals = [x for x in rows if x.get("phase") == "cross_final"]
    return finals[-1] if finals else (rows[-1] if rows else {})


def build_report(out: Path, guest_rows: list[dict], host_rows: list[dict]) -> dict:
    intra = [x for x in guest_rows if x.get("experiment") == "intra"]
    first = intra[0] if intra else {}
    last = intra[-1] if intra else {}
    final = cross_final(host_rows)
    categories = final.get("categories", {})
    ranked = sorted(
        ({"category": name, **metrics} for name, metrics in categories.items()),
        key=lambda x: x.get("physical_sharing_savings_kb", 0), reverse=True,
    )
    paths = sorted(
        ({"path": name, **metrics} for name, metrics in final.get("paths", {}).items()),
        key=lambda x: x.get("physical_sharing_savings_kb", 0), reverse=True,
    )[:100]

    common_files = collections.defaultdict(list)
    cross_guests_path = out / "cross-guest-final.json"
    if cross_guests_path.exists():
        for session in json.loads(cross_guests_path.read_text()):
            for item in session.get("guest", {}).get("top_resident_files", []):
                common_files[item["path"]].append({
                    "session": session["session"], "workflow": session["workflow"],
                    "resident_kb": item["resident_kb"], "category": item["category"],
                })
    common = []
    for path, uses in common_files.items():
        if len(uses) >= 2:
            common.append({
                "path": path, "sessions": len(uses),
                "logical_resident_kb": sum(x["resident_kb"] for x in uses),
                "category": uses[0]["category"], "uses": uses,
            })
    common.sort(key=lambda x: x["logical_resident_kb"], reverse=True)

    return {
        "intra": {
            "boot": first,
            "final": last,
            "turn_snapshots": len(intra),
        },
        "cross": {
            "resident_sessions": final.get("resident_sessions", 0),
            "host_categories_ranked_by_physical_sharing": ranked,
            "host_paths_ranked_by_physical_sharing": paths,
            "common_guest_resident_files_logically_reused_but_physically_duplicated": common[:100],
        },
    }


def plot(out: Path) -> None:
    guest_rows = load_jsonl(out / "guest-snapshots.jsonl")
    host_rows = load_jsonl(out / "cross-host-snapshots.jsonl")
    turns, series = intra_series(guest_rows)
    final = cross_final(host_rows)
    categories = final.get("categories", {})

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
    ax = axes[0]
    colors = ["tab:blue", "tab:orange", "tab:green", "tab:red", "tab:purple"]
    for (name, values), color in zip(series.items(), colors):
        ax.plot(turns, values, linewidth=2, label=name, color=color)
    ax.set_xlabel("Turn")
    ax.set_ylabel("Memory (MiB)")
    ax.set_title("State retained across turns in one microVM")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.2)

    ax = axes[1]
    ranked = sorted(
        categories.items(), key=lambda x: x[1].get("physical_sharing_savings_kb", 0), reverse=True
    )[:10]
    labels = [x[0].replace("_", " ") for x in ranked]
    rss = [mib(x[1].get("rss_kb", 0)) for x in ranked]
    pss = [mib(x[1].get("pss_kb", 0)) for x in ranked]
    y = list(range(len(labels)))
    ax.barh(y, rss, color="lightsteelblue", label="Summed RSS")
    ax.barh(y, pss, color="tab:blue", label="Summed PSS")
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.set_xlabel("Memory across sessions (MiB)")
    ax.set_title("Physical sharing across co-resident sessions")
    ax.legend(fontsize=8)
    ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    fig.savefig(out / "pss-reuse-overview.png", dpi=180)
    plt.close(fig)

    # A readable view of retained state within one persistent sandbox.  The
    # overview intentionally includes the whole VM; this companion plot
    # separates guest kernel caches from named resident-file categories.
    intra_rows = sorted(
        (x for x in guest_rows if x.get("experiment") == "intra"),
        key=lambda x: x["turn"],
    )
    intra_turns = [x["turn"] for x in intra_rows]
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
    ax = axes[0]
    for key, label, color in (
        ("Cached", "Guest page cache", "tab:green"),
        ("Slab", "Guest slab", "tab:red"),
        ("AnonPages", "Guest anonymous pages", "tab:purple"),
        ("Mapped", "Guest mapped pages", "tab:orange"),
    ):
        ax.plot(
            intra_turns,
            [mib(x.get("guest", {}).get("meminfo_kb", {}).get(key, 0)) for x in intra_rows],
            linewidth=2,
            label=label,
            color=color,
        )
    ax.set_xlabel("Turn")
    ax.set_ylabel("Resident memory (MiB)")
    ax.set_title("Guest state retained across turns")
    ax.grid(alpha=0.2)
    ax.legend(fontsize=8)

    ax = axes[1]
    for key, label in FILE_CATEGORY_LABELS.items():
        ax.plot(
            intra_turns,
            [mib(x.get("guest", {}).get("file_cache_categories", {}).get(key, {}).get("resident_kb", 0)) for x in intra_rows],
            linewidth=2,
            label=label,
        )
    ax.set_xlabel("Turn")
    ax.set_ylabel("Resident file pages (MiB)")
    ax.set_title("Which files remain warm")
    ax.grid(alpha=0.2)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "intra-turn-state-reuse.png", dpi=180)
    plt.close(fig)

    # Cross-session physical sharing must be read from host PSS.  Guest file
    # residency is shown separately because identical guest cache contents are
    # still backed by private anonymous guest RAM without KSM/page dedup.
    ranked_all = sorted(
        final.get("categories", {}).items(),
        key=lambda x: x[1].get("rss_kb", 0),
        reverse=True,
    )
    labels = [name.replace("_", " ") for name, _ in ranked_all]
    rss = [mib(metrics.get("rss_kb", 0)) for _, metrics in ranked_all]
    pss = [mib(metrics.get("pss_kb", 0)) for _, metrics in ranked_all]
    savings = [max(0.0, a - b) for a, b in zip(rss, pss)]
    y = list(range(len(labels)))

    final_guests = []
    final_guest_path = out / "cross-guest-final.json"
    if final_guest_path.exists():
        final_guests = json.loads(final_guest_path.read_text())
    logical_categories = collections.defaultdict(float)
    for session in final_guests:
        for category, metrics in session.get("guest", {}).get("file_cache_categories", {}).items():
            logical_categories[category] += mib(metrics.get("resident_kb", 0))
    logical_ranked = sorted(logical_categories.items(), key=lambda x: x[1], reverse=True)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
    ax = axes[0]
    ax.barh(y, rss, color="lightsteelblue", label="Summed RSS")
    ax.barh(y, pss, color="tab:blue", label="Summed PSS")
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.set_xlabel("Host memory across five sessions (MiB)")
    ax.set_title("Physical sharing: RSS versus PSS")
    ax.grid(axis="x", alpha=0.2)
    ax.legend(fontsize=8)
    for yi, saved in zip(y, savings):
        if saved >= 0.1:
            ax.text(max(rss[yi], 0.05) * 1.08, yi, f"{saved:.1f} MiB saved", va="center", fontsize=8)

    ax = axes[1]
    guest_labels = [FILE_CATEGORY_LABELS.get(k, k.replace("_", " ")) for k, _ in logical_ranked]
    guest_values = [v for _, v in logical_ranked]
    gy = list(range(len(guest_labels)))
    ax.barh(gy, guest_values, color="tab:orange")
    ax.set_yticks(gy, guest_labels)
    ax.invert_yaxis()
    ax.set_xlabel("Summed resident pages inside guests (MiB)")
    ax.set_title("Logically repeated, physically private guest caches")
    ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    fig.savefig(out / "cross-session-state-sharing.png", dpi=180)
    plt.close(fig)

    report = build_report(out, guest_rows, host_rows)
    (out / "reuse-report.json").write_text(json.dumps(report, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plot(args.output)
    print(args.output / "pss-reuse-overview.png")


if __name__ == "__main__":
    main()
