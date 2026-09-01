"""Turns a device's raw `metrics` dict (keys from config's trend_metrics)
into human-readable text — used by both the Markdown report and the web
API, so "information_transfer_rate=40, longevity_days=730" (code-shaped)
becomes "Information transfer rate: 40 bits/min · Implant longevity: 730
days in vivo" (reader-shaped) everywhere, not just in one place.
"""
from __future__ import annotations


def metric_label(key: str, trend_metrics: list[dict]) -> str:
    meta = next((m for m in trend_metrics if m["key"] == key), None)
    return meta["label"] if meta else key.replace("_", " ").capitalize()


def metric_unit(key: str, trend_metrics: list[dict]) -> str:
    meta = next((m for m in trend_metrics if m["key"] == key), None)
    return (meta or {}).get("unit", "")


def format_metrics(metrics: dict, trend_metrics: list[dict], sep: str = " · ") -> str:
    parts = []
    for key, value in metrics.items():
        label = metric_label(key, trend_metrics)
        unit = metric_unit(key, trend_metrics)
        value_str = f"{value:,}" if isinstance(value, (int, float)) else str(value)
        parts.append(f"{label}: {value_str}" + (f" {unit}" if unit else ""))
    return sep.join(parts) if parts else "—"
