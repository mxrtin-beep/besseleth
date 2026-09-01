"""Renders industry-trend charts (e.g. information transfer rate vs.
implant longevity, colored by FDA status) from devices.yaml, saved as PNGs
that report.py embeds in the weekly report.

Requires matplotlib (in requirements.txt). If it's missing, chart
generation is skipped with a warning rather than breaking the report.
"""
from __future__ import annotations

from pathlib import Path

from .store import Device

_FDA_COLORS = {
    "pma approved": "#2a9d8f",
    "fda approved": "#2a9d8f",
    "breakthrough device designation": "#e9c46a",
    "ide approved": "#f4a261",
    "510(k) cleared": "#457b9d",
    "unknown": "#adb5bd",
}


def _color_for_fda_status(status: str) -> str:
    return _FDA_COLORS.get((status or "unknown").strip().lower(), "#adb5bd")


def plot_metric_scatter(
    devices: list[Device],
    x_key: str,
    y_key: str,
    x_label: str,
    y_label: str,
    title: str,
    out_path: str | Path,
) -> Path | None:
    """Scatter plot of two numeric metrics (e.g. ITR vs. longevity),
    point color = FDA status, marker shape = industry vs academic."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[trends] matplotlib not installed; skipping chart. `pip install matplotlib`.")
        return None

    points = [d for d in devices if x_key in d.metrics and y_key in d.metrics]
    if not points:
        print(f"[trends] No devices have both '{x_key}' and '{y_key}'; skipping {out_path}.")
        return None

    fig, ax = plt.subplots(figsize=(7, 5))
    for d in points:
        marker = "o" if d.org_type == "industry" else "^"
        ax.scatter(
            d.metrics[x_key],
            d.metrics[y_key],
            c=_color_for_fda_status(d.fda_status),
            marker=marker,
            s=90,
            edgecolors="black",
            linewidths=0.5,
            zorder=3,
        )
        ax.annotate(d.name, (d.metrics[x_key], d.metrics[y_key]), fontsize=8, xytext=(5, 5), textcoords="offset points")

    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_title(title)
    ax.grid(True, alpha=0.3, zorder=0)

    # Legend: marker shape = org type, color = FDA status actually present.
    from matplotlib.lines import Line2D

    legend_handles = [
        Line2D([0], [0], marker="o", color="w", markerfacecolor="gray", markeredgecolor="black", label="Industry", markersize=8),
        Line2D([0], [0], marker="^", color="w", markerfacecolor="gray", markeredgecolor="black", label="Academic", markersize=8),
    ]
    statuses_present = sorted({(d.fda_status or "unknown") for d in points})
    for status in statuses_present:
        legend_handles.append(
            Line2D([0], [0], marker="s", color="w", markerfacecolor=_color_for_fda_status(status), label=status, markersize=8)
        )
    ax.legend(handles=legend_handles, fontsize=7, loc="best")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def plot_fda_status_counts(devices: list[Device], out_path: str | Path) -> Path | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[trends] matplotlib not installed; skipping chart.")
        return None

    if not devices:
        return None

    counts: dict[str, int] = {}
    for d in devices:
        status = d.fda_status or "unknown"
        counts[status] = counts.get(status, 0) + 1

    fig, ax = plt.subplots(figsize=(7, 4))
    labels = list(counts.keys())
    values = [counts[l] for l in labels]
    colors = [_color_for_fda_status(l) for l in labels]
    ax.bar(labels, values, color=colors, edgecolor="black", linewidth=0.5)
    ax.set_ylabel("# of tracked devices")
    ax.set_title("Devices by FDA regulatory status")
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right", fontsize=8)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def generate_trend_charts(devices: list[Device], trend_metrics: list[dict], out_dir: str | Path) -> list[Path]:
    """Generates a scatter for every pair of numeric metrics defined in
    config's industry.trend_metrics, plus an FDA-status bar chart. Returns
    the list of PNG paths actually written."""
    out_dir = Path(out_dir)
    generated = []

    numeric_keys = [m["key"] for m in trend_metrics if m.get("type", "numeric") == "numeric"]
    for i, x_key in enumerate(numeric_keys):
        for y_key in numeric_keys[i + 1 :]:
            x_meta = next(m for m in trend_metrics if m["key"] == x_key)
            y_meta = next(m for m in trend_metrics if m["key"] == y_key)
            path = plot_metric_scatter(
                devices,
                x_key,
                y_key,
                f"{x_meta['label']} ({x_meta.get('unit', '')})",
                f"{y_meta['label']} ({y_meta.get('unit', '')})",
                f"{y_meta['label']} vs. {x_meta['label']}",
                out_dir / f"{x_key}_vs_{y_key}.png",
            )
            if path:
                generated.append(path)

    fda_chart = plot_fda_status_counts(devices, out_dir / "fda_status.png")
    if fda_chart:
        generated.append(fda_chart)

    return generated
