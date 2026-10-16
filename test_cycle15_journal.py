"""Cycle 15 tests: flight-recorder journal (seq, redaction, bound)."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.journal import JournalRecorder


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_journal_seq_and_redaction(tmp_path):
    path = str(tmp_path / "j.jsonl")
    rec = JournalRecorder(path)
    rec.on_event({"method": "Network.requestWillBeSent", "sessionId": "s1", "params": {
        "requestId": "r1",
        "request": {"url": "https://x/api", "headers": {"Cookie": "sid=secret", "Accept": "application/json"}},
    }})
    rec.on_event({"method": "Network.responseReceived", "sessionId": "s1", "params": {
        "response": {"headers": {"Set-Cookie": "a=b-secret", "Content-Type": "application/json"}},
    }})
    rec.close()
    rows = _read(path)
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[0]["method"] == "Network.requestWillBeSent"
    blob = json.dumps(rows)
    assert "sid=secret" not in blob and "a=b-secret" not in blob
    assert "[redacted]" in blob
    assert rec.stats()["seq"] == 2


def test_journal_truncates_at_max_bytes(tmp_path):
    path = str(tmp_path / "small.jsonl")
    rec = JournalRecorder(path, max_bytes=200)
    for i in range(20):
        rec.on_event({"method": "Network.dataReceived", "params": {"requestId": f"r{i}", "data": "x" * 50}})
    rec.close()
    rows = _read(path)
    assert any(r["method"] == "__journal_truncated__" for r in rows)
    assert rec.dropped >= 1
