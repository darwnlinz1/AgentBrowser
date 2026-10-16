"""Cycle 5 tests: self-healing fingerprints. No browser launched."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.dom import DOMEngine
from agent_browser.fingerprints import ElementFingerprint, resolve_fingerprint, similarity


def _el(**kw):
    base = {"tag": "button", "role": "button", "text": "Buy now", "id": "buy",
            "attributes": {"data-testid": "buy-btn"}, "rect": {"x": 10, "y": 10, "width": 80, "height": 30}}
    base.update(kw)
    return ElementFingerprint.from_snapshot_element(base)


def test_similarity_and_heal_after_id_change():
    original = _el()
    # Re-render renamed the id and moved the button slightly; test-id survives.
    moved = _el(id="buy-v2", rect={"x": 14, "y": 12, "width": 80, "height": 30})
    other = _el(id="cancel", text="Cancel", attributes={}, rect={"x": 300, "y": 10, "width": 80, "height": 30})
    assert similarity(original, moved) > similarity(original, other)
    out = resolve_fingerprint(original, [moved, other])
    assert out["healed"] is True
    assert out["match"] is moved
    assert out["evidence"]["kind"] == "fingerprint_heal"
    assert out["evidence"]["from"]["element_id"] == "buy"


def test_below_threshold_fails_closed():
    original = _el()
    unrelated = ElementFingerprint(role="link", name="Home", tag="a", text="Home")
    out = resolve_fingerprint(original, [unrelated])
    assert out["healed"] is False
    assert out["match"] is None
    assert out["evidence"]["kind"] == "fingerprint_no_match"


class _FakeCDP:
    async def send(self, method, params=None, session_id=None, timeout=15):
        expr = (params or {}).get("expression", "")
        if "__agent_internal_scanner__" not in expr:
            return {"result": {"value": None}}
        # Interactive snapshot expression
        return {"result": {"value": {"elements": [
            {"ref": 3, "tag": "button", "role": "button", "text": "Buy now", "id": "buy-v2",
             "attributes": {"data-testid": "buy-btn"}, "rect": {"x": 12, "y": 11, "width": 80, "height": 30}},
            {"ref": 4, "tag": "button", "role": "button", "text": "Cancel", "id": "cancel",
             "attributes": {}, "rect": {"x": 300, "y": 10, "width": 80, "height": 30}},
        ], "elementCount": 2}}}


@pytest.mark.asyncio
async def test_dom_resolve_healed_ref():
    dom = DOMEngine(_FakeCDP(), lambda: "sid")
    dom._fingerprints["#buy"] = _el()
    healed = await dom.resolve_healed_ref("#buy")
    assert healed is not None
    assert healed["ref"] == 3
    assert healed["confidence"] >= 0.45
    assert dom.heal_log[-1]["kind"] == "fingerprint_heal"
    # Unknown selector: nothing stored, no heal attempted.
    assert await dom.resolve_healed_ref("#never-seen") is None
