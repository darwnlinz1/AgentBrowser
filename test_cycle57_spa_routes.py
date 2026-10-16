"""Cycle 57 tests: SPA route windows (route epochs) in quiescence."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.quiescence import NetworkActivityMonitor, QuiescenceTracker


def _req(rid, url="https://x/api"):
    return {"method": "Network.requestWillBeSent",
            "params": {"requestId": rid, "type": "Fetch",
                       "request": {"url": url, "method": "GET"}}}


def test_monitor_tags_requests_with_route_epoch():
    mon = NetworkActivityMonitor()
    mon.on_event(_req("r1"))
    mon.on_event({"method": "Page.navigatedWithinDocument", "params": {"frameId": "f"}})
    mon.on_event(_req("r2"))
    snap = mon.snapshot()
    assert snap["route_epoch"] == 1
    assert snap["in_flight"] == ["r1", "r2"]
    assert snap["in_flight_current_route"] == ["r2"]
    assert snap["in_flight_previous_routes"] == 1
    mon.on_event({"method": "Network.loadingFinished", "params": {"requestId": "r1"}})
    assert mon.snapshot()["in_flight_previous_routes"] == 0


def test_iframe_navigation_does_not_open_route_window():
    mon = NetworkActivityMonitor()
    mon.on_event({"method": "Page.frameNavigated",
                  "params": {"frame": {"parentId": "parent-frame"}}})
    assert mon.route_epoch == 0
    mon.on_event({"method": "Page.frameNavigated",
                  "params": {"frame": {"id": "main"}}})
    assert mon.route_epoch == 1


def test_tracker_route_change_unblocks_previous_route_stragglers():
    t = QuiescenceTracker(quiet_window_s=0.3)
    t.note_request_start("r1", now=100.0)
    assert t.is_quiet(now=200.0) is False  # r1 still in flight
    t.note_route_change(now=200.0)
    assert t.route_epoch == 1
    assert t.is_quiet(now=200.1) is False  # route change itself is activity
    assert t.is_quiet(now=200.31) is True  # stale r1 no longer blocks
    snap = t.snapshot(now=200.31)
    assert snap["stale_in_flight"] == ["r1"]
    assert snap["in_flight"] == []
    # New-route activity still blocks normally.
    t.note_request_start("r2", now=201.0)
    assert t.is_quiet(now=202.0) is False
    t.note_request_end("r2", now=202.0)
    assert t.is_quiet(now=202.31) is True
    # A stale request finishing clears it from reporting (r1 never
    # finished, so it correctly remains listed as a previous-route stale).
    t.note_request_start("r3", now=203.0)
    t.note_route_change(now=203.1)
    t.note_request_end("r3", now=203.2)
    assert t.snapshot(now=203.2)["stale_in_flight"] == ["r1"]
