#!/usr/bin/env python3
"""Plot paired node0-only vs TPP latency bars for matched sessions.

By default, reads the per-session comparison CSV and writes a JPEG beside it:

    python3 plot_per_session_latency.py

Alternate files can be supplied with --input-csv and --output.
"""

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parent
DEFAULT_CSV = ROOT / "traces/remote-swebench-scale-no-boot15/per-session-latency-comparison.csv"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    required = {
        "session",
        "instance_id",
        "node0_e2e_seconds",
        "tpp_e2e_seconds",
        "node0_workflow_seconds",
        "tpp_workflow_seconds",
    }
    missing = required.difference(rows[0].keys() if rows else ())
    if missing:
        raise ValueError(f"{path} is missing required CSV columns: {', '.join(sorted(missing))}")
    if not rows:
        raise ValueError(f"{path} contains no session rows")
    return rows


def plot(rows: list[dict[str, str]], output: Path, dpi: int = 180) -> None:
    positions = np.arange(len(rows))
    width = 0.36
    labels = [f"{int(row['session']):02d}" for row in rows]

    series = (
        ("node0_e2e_seconds", "tpp_e2e_seconds", "Launch-to-finish latency", "Latency (seconds)"),
        ("node0_workflow_seconds", "tpp_workflow_seconds", "Workflow runtime", "Runtime (seconds)"),
    )
    colors = {"node0": "#2878B5", "tpp": "#E1812C"}
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True, constrained_layout=True)

    for ax, (node0_key, tpp_key, title, ylabel) in zip(axes, series):
        node0 = np.array([float(row[node0_key]) for row in rows])
        tpp = np.array([float(row[tpp_key]) for row in rows])
        ax.bar(positions - width / 2, node0, width, color=colors["node0"], label="Node0-only (no TPP)")
        ax.bar(positions + width / 2, tpp, width, color=colors["tpp"], label="TPP enabled")
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.grid(axis="y", alpha=0.25)
        ax.set_axisbelow(True)

        if node0_key == "node0_e2e_seconds":
            for x, n_value, t_value in zip(positions, node0, tpp):
                delta = t_value - n_value
                ax.annotate(
                    f"{delta:+.1f}s",
                    (x + width / 2, max(n_value, t_value)),
                    xytext=(0, 3),
                    textcoords="offset points",
                    ha="center",
                    va="bottom",
                    fontsize=7,
                    color="#444444",
                )

    mean_node0 = np.mean([float(row["node0_e2e_seconds"]) for row in rows])
    mean_tpp = np.mean([float(row["tpp_e2e_seconds"]) for row in rows])
    fig.suptitle(
        f"Per-session latency: TPP vs node0-only\n"
        f"Matched sessions: {len(rows)} · mean end-to-end {mean_node0:.2f}s vs {mean_tpp:.2f}s "
        f"({mean_tpp - mean_node0:+.2f}s with TPP) · Blue: node0-only · Orange: TPP",
        fontsize=15,
    )
    axes[-1].set_xticks(positions, labels)
    axes[-1].set_xlabel("Matched session number (same deterministic workload order)")

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=dpi, format=output.suffix.lstrip(".") or "jpg")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_CSV, help="paired per-session latency CSV")
    parser.add_argument("--output", type=Path, help="image path (defaults to per-session-latency-paired-bars.jpg beside CSV)")
    parser.add_argument("--dpi", type=int, default=180, help="output resolution (default: 180)")
    args = parser.parse_args()
    output = args.output or args.input_csv.with_name("per-session-latency-paired-bars.jpg")
    plot(read_rows(args.input_csv), output, dpi=args.dpi)
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
