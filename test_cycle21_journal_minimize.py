"""Cycle 21 tests: merge_bodies + async CDD (no browser)."""

import asyncio
import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.journal_minimize import merge_bodies
from agent_browser.minimizer import cdd_async


def test_merge_bodies_attaches_network_bodies():
    journal_ex = [{
        "uid": "x:r1", "method": "GET", "url": "https://app.example/api/fail",
        "status": None, "resp_headers": [], "body_b64": "", "servable": False,
        "resource_type": "Fetch",
    }]
    entries = [{
        "id": "r1", "method": "GET", "url": "https://app.example/api/fail", "status": 500,
        "response_headers": {"Content-Type": "application/json"},
        "response_body": '{"error":"x"}', "resource_type": "Fetch",
    }]
    merged = merge_bodies(journal_ex, entries)
    assert merged[0]["status"] == 500
    assert merged[0]["servable"] is True
    assert base64.b64decode(merged[0]["body_b64"]).decode() == '{"error":"x"}'


@pytest.mark.asyncio
async def test_cdd_async_isolates_essentials():
    units = ([{"uid": f"n{i}", "url": f"https://x/n{i}", "prior": 0.03} for i in range(12)]
             + [{"uid": "doc", "url": "https://x/", "prior": 0.9},
                {"uid": "api", "url": "https://x/api/fail", "prior": 0.95}])

    async def predicate(candidate):
        await asyncio.sleep(0)
        return {"doc", "api"} <= {u["uid"] for u in candidate}

    minimal = await cdd_async(units, predicate)
    assert sorted(u["uid"] for u in minimal) == ["api", "doc"]
