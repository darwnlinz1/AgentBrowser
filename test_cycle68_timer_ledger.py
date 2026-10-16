"""Cycle 68 tests: opt-in observe-only timer ledger."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.timer_ledger import (
    TIMER_LEDGER_JS, ledger_enabled_from_env, parse_ledger, timers_block_settle,
)
from agent_browser.tracer import ActionTracer


def test_ledger_js_is_observe_only():
    # Delegates to the originals and never fabricates delays.
    assert "origSetTimeout" in TIMER_LEDGER_JS and "origSetInterval" in TIMER_LEDGER_JS
    assert "__agent_internal_timer_ledger__" in TIMER_LEDGER_JS
    assert "ledger.timeouts.set" in TIMER_LEDGER_JS


def test_parse_and_block_decisions():
    assert parse_ledger(None)["installed"] is False
    assert parse_ledger({"installed": False})["pending_timeouts"] == 0
    soon = parse_ledger({"installed": True, "pending_timeouts": 1,
                         "pending_intervals": 0, "next_fire_in_ms": 40})
    assert timers_block_settle(soon) is True
    later = parse_ledger({"installed": True, "pending_timeouts": 1,
                          "pending_intervals": 0, "next_fire_in_ms": 5000})
    assert timers_block_settle(later) is False
    heartbeat_only = parse_ledger({"installed": True, "pending_timeouts": 0,
                                   "pending_intervals": 3, "next_fire_in_ms": None})
    assert timers_block_settle(heartbeat_only) is False  # intervals never block
    assert timers_block_settle(parse_ledger(None)) is False


def test_env_gate_default_off(monkeypatch):
    monkeypatch.delenv("AGENTBROWSER_TIMER_LEDGER", raising=False)
    assert ledger_enabled_from_env() is False
    monkeypatch.setenv("AGENTBROWSER_TIMER_LEDGER", "1")
    assert ledger_enabled_from_env() is True
    monkeypatch.setenv("AGENTBROWSER_TIMER_LEDGER", "off")
    assert ledger_enabled_from_env() is False


class _FakeRuntime:
    def __init__(self, value):
        self._value = value
        self.expressions = []

    async def evaluate(self, expression, **kwargs):
        self.expressions.append(expression)
        return {"value": self._value}


@pytest.mark.asyncio
async def test_timer_probe_parses_runtime_value():
    tracer = ActionTracer(None, None, None, _FakeRuntime(
        {"installed": True, "pending_timeouts": 2, "pending_intervals": 1,
         "next_fire_in_ms": 12}), lambda: "S1", timer_ledger=True)
    ledger = await tracer._timer_probe()
    assert ledger == {"installed": True, "pending_timeouts": 2,
                      "pending_intervals": 1, "next_fire_in_ms": 12}
    assert timers_block_settle(ledger) is True


@pytest.mark.asyncio
async def test_ensure_ledger_noop_when_disabled():
    tracer = ActionTracer(None, None, None, _FakeRuntime(None), lambda: "S1",
                          timer_ledger=False)
    await tracer._ensure_timer_ledger("S1")  # must not touch CDP (None)
    assert tracer._ledger_sessions == set()
