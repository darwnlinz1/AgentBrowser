"""Cycle 53 tests: narrowed Fetch.enable patterns derived from rules."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.network import InterceptRule, NetworkEngine, derive_cdp_url_pattern


@pytest.mark.parametrize("pattern,expected", [
    (r"/api/failing-endpoint", "*/api/failing-endpoint*"),
    (r".*/api/data.*", "*/api/data*"),
    (r"^https://example\.com/api/users$", "*https://example.com/api/users*"),
    (r"/api/data", "*/api/data*"),
])
def test_derivable_patterns(pattern, expected):
    assert derive_cdp_url_pattern(pattern) == expected


@pytest.mark.parametrize("pattern", [
    r"/api/(users|items)",   # alternation: literal run not guaranteed
    r"/api/\d+",             # class escape + quantifier
    r"/api/[a-z]+/data",     # character class
    r"ab",                   # literal run too short to be worth narrowing
    r"https://x.co/a?b=1",   # '?' quantifier ambiguity
    "",
    None,
])
def test_non_derivable_patterns_fall_back(pattern):
    assert derive_cdp_url_pattern(pattern) is None


def test_derived_glob_is_superset_of_regex_matches():
    import fnmatch
    import re

    for pattern in (r"/api/failing-endpoint", r".*/api/data.*",
                    r"^https://example\.com/api/users$"):
        glob = derive_cdp_url_pattern(pattern)
        rx = re.compile(pattern)
        for url in ("http://h/api/failing-endpoint?x=1",
                    "http://h/api/data",
                    "https://example.com/api/users"):
            if rx.search(url):
                assert fnmatch.fnmatch(url, glob), (pattern, glob, url)


class _FakeCDP:
    def __init__(self):
        self.sent = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.sent.append((method, params))
        return {}


def _engine():
    return NetworkEngine(_FakeCDP(), lambda: "S1")


@pytest.mark.asyncio
async def test_enable_interception_narrows_from_rules():
    eng = _engine()
    eng.add_rule(InterceptRule(rule_id="a", url_pattern=r"/api/failing-endpoint",
                               action="fulfill", mock_body="{}"))
    eng.add_rule(InterceptRule(rule_id="b", url_pattern=r"/api/data",
                               action="fulfill", mock_body="{}"))
    patterns = await eng.enable_interception()
    assert {"urlPattern": "*/api/failing-endpoint*", "requestStage": "Request"} in patterns
    assert {"urlPattern": "*/api/data*", "requestStage": "Request"} in patterns
    assert all(p["urlPattern"] != "*" for p in patterns)
    sent = dict(eng.cdp.sent)["Fetch.enable"]
    assert sent["patterns"] == patterns


@pytest.mark.asyncio
async def test_one_wildcard_rule_widens_everything():
    eng = _engine()
    eng.add_rule(InterceptRule(rule_id="a", url_pattern=r"/api/data",
                               action="fulfill", mock_body="{}"))
    eng.add_rule(InterceptRule(rule_id="b", url_pattern=r"/api/(x|y)",
                               action="fulfill", mock_body="{}"))
    patterns = await eng.enable_interception()
    assert patterns == [{"urlPattern": "*", "requestStage": "Request"}]


@pytest.mark.asyncio
async def test_no_rules_and_response_stage_stay_wide():
    eng = _engine()
    assert await eng.enable_interception() == [
        {"urlPattern": "*", "requestStage": "Request"}]
    assert await eng.enable_interception(include_response=True) == [
        {"urlPattern": "*", "requestStage": "Request"},
        {"urlPattern": "*", "requestStage": "Response"}]
