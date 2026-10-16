"""
Self-healing element fingerprints.

Refs and CSS selectors die on every re-render and deploy. A fingerprint keeps
several independent signals about an element (role, accessible name, text,
tag, id, test-id, placeholder, href, rough position); when the primary
locator breaks, candidates are ranked by weighted similarity and the best
match above threshold wins. Every heal is returned as evidence (old -> new,
score) and assertions must fail closed below the confidence threshold --
a healer that silently guesses is worse than a clear failure.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ElementFingerprint:
    role: str = ""
    name: str = ""
    text: str = ""
    tag: str = ""
    element_id: str = ""
    test_id: str = ""
    placeholder: str = ""
    href: str = ""
    input_type: str = ""
    rect: Optional[Dict[str, float]] = None

    @classmethod
    def from_snapshot_element(cls, el: Dict[str, Any]) -> "ElementFingerprint":
        """Build from an INTERACTIVE_SNAPSHOT_JS element or an AX ref entry."""
        attrs = el.get("attributes") or {}
        return cls(
            role=(el.get("role") or "").lower(),
            name=(el.get("ariaLabel") or el.get("name") or "")[:120],
            text=(el.get("text") or "")[:120],
            tag=(el.get("tag") or "").lower(),
            element_id=el.get("id") or "",
            test_id=attrs.get("data-testid") or attrs.get("data-test-id") or el.get("test_id") or "",
            placeholder=el.get("placeholder") or "",
            href=el.get("href") or "",
            input_type=el.get("type") or "",
            rect=el.get("rect"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_WEIGHTS = {
    "test_id": 0.30,
    "element_id": 0.20,
    "role": 0.15,
    "name": 0.15,
    "text": 0.10,
    "tag": 0.05,
    "placeholder": 0.05,
    "href": 0.05,
    "position": 0.05,
}


def _text_sim(a: str, b: str) -> float:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.7
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


def _position_sim(a: Optional[Dict[str, float]], b: Optional[Dict[str, float]]) -> float:
    if not a or not b:
        return 0.0
    try:
        ax, ay = a["x"] + a["width"] / 2, a["y"] + a["height"] / 2
        bx, by = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    except Exception:
        return 0.0
    dist = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
    return max(0.0, 1.0 - dist / 600.0)  # 600px away scores 0


def similarity(fp: ElementFingerprint, candidate: ElementFingerprint) -> float:
    score = 0.0
    if fp.test_id and candidate.test_id and fp.test_id == candidate.test_id:
        score += _WEIGHTS["test_id"]
    if fp.element_id and candidate.element_id and fp.element_id == candidate.element_id:
        score += _WEIGHTS["element_id"]
    if fp.role and candidate.role and fp.role == candidate.role:
        score += _WEIGHTS["role"]
    score += _WEIGHTS["name"] * _text_sim(fp.name, candidate.name)
    score += _WEIGHTS["text"] * _text_sim(fp.text, candidate.text)
    if fp.tag and candidate.tag and fp.tag == candidate.tag:
        score += _WEIGHTS["tag"]
    if fp.placeholder and candidate.placeholder and fp.placeholder == candidate.placeholder:
        score += _WEIGHTS["placeholder"]
    if fp.href and candidate.href and fp.href == candidate.href:
        score += _WEIGHTS["href"]
    score += _WEIGHTS["position"] * _position_sim(fp.rect, candidate.rect)
    return round(min(score, 1.0), 4)


def resolve_fingerprint(
    fp: ElementFingerprint,
    candidates: List[ElementFingerprint],
    *,
    threshold: float = 0.45,
) -> Dict[str, Any]:
    """
    Rank candidates for a (presumably broken) fingerprint.

    Returns the best match with its confidence, the ranked shortlist, and
    whether this counts as a heal. Below ``threshold`` nothing is healed:
    the caller must fail closed / re-snapshot rather than click a guess.
    """
    ranked = sorted(
        ({"candidate": c, "score": similarity(fp, c)} for c in candidates),
        key=lambda r: r["score"],
        reverse=True,
    )
    best = ranked[0] if ranked else None
    healed = bool(best and best["score"] >= threshold)
    return {
        "healed": healed,
        "confidence": best["score"] if best else 0.0,
        "match": best["candidate"] if healed else None,
        "ranked": ranked[:5],
        "threshold": threshold,
        "evidence": {
            "kind": "fingerprint_heal" if healed else "fingerprint_no_match",
            "from": fp.to_dict(),
            "to": best["candidate"].to_dict() if healed else None,
            "score": best["score"] if best else 0.0,
        },
    }
