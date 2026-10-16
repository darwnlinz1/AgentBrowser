"""Cycle 18 tests: journal loading, exchange pairing, evidence priors."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.journal import build_exchanges, load_journal
from agent_browser.replay import build_units, exchange_prior


def test_journal_to_exchanges(tmp_path):
    path = tmp_path / "j.jsonl"
    rows = [
        {"seq": 2, "method": "Network.responseReceived", "params": {"requestId": "r1", "response": {"status": 500, "headers": {"Content-Type": "application/json"}}}},
        {"seq": 1, "method": "Network.requestWillBeSent", "params": {"requestId": "r1", "type": "Fetch", "request": {"url": "https://x/api/fail", "method": "GET"}}},
        {"seq": 3, "method": "Network.requestWillBeSent", "params": {"requestId": "r2", "type": "Image", "request": {"url": "https://x/logo.png", "method": "GET"}}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    events = load_journal(str(path))
    assert [e["seq"] for e in events] == [1, 2, 3]  # sorted by seq
    exchanges = build_exchanges(events)
    assert exchanges[0]["status"] == 500
    assert exchanges[0]["servable"] is False  # bodies are not CDP events
    units = build_units(exchanges, target_url="https://x/api/fail")
    priors = {u["url"]: u["prior"] for u in units}
    assert priors["https://x/api/fail"] == 0.95
    assert priors["https://x/logo.png"] == 0.02


def test_exchange_prior_ordering():
    assert exchange_prior({"url": "https://x/api/background-ping", "status": 200}) == 0.03
    assert exchange_prior({"url": "https://x/api/data", "status": 500}) == 0.8
    assert exchange_prior({"url": "https://x/app.js", "status": 200, "resource_type": "Script"}) == 0.3
