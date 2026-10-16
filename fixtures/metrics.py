"""Pure scoring helpers for the fault corpus (Chrome-free, unit-testable)."""

from __future__ import annotations

from typing import Dict, Iterable, Set


def attribution_scores(predicted: Set[str], truth: Set[str]) -> Dict[str, float]:
    """Precision/recall/F1 for a set of attributed request paths vs ground truth."""
    tp = len(predicted & truth)
    fp = len(predicted - truth)
    fn = len(truth - predicted)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def dropped_event_rate(dropped: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(dropped / total, 6)


def false_supported_rate(links: Iterable[dict], background_paths: Iterable[str]) -> float:
    """Fraction of supported/attributed links that point at known background noise (target 0)."""
    bg = set(background_paths)
    links = list(links)
    if not links:
        return 0.0
    bad = 0
    for link in links:
        if link.get("confidence") not in ("supported", "attributed", "proven", "interventional"):
            continue
        url = str((link.get("evidence") or {}).get("url") or link.get("url") or "")
        if any(p in url for p in bg):
            bad += 1
    return round(bad / len(links), 4)
