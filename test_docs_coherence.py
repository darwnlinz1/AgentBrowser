"""Docs coherence: documented CLI commands must exist in the real CLI."""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

ROOT = os.path.abspath(os.path.dirname(__file__))


def _cli_commands():
    src = open(os.path.join(ROOT, "agent_browser", "cli.py"), encoding="utf-8").read()
    cmds = set(re.findall(r'add_parser\("([^"]+)"', src))
    # Intercept subcommands (add/clear) are not top-level commands.
    return cmds - {"add", "clear"}


def _documented(path):
    text = open(path, encoding="utf-8").read()
    return set(re.findall(r"agent_browser ([a-z][a-z0-9-]*)", text))


def test_skill_documents_only_real_commands():
    documented = _documented(os.path.join(ROOT, ".agents", "skills", "browser", "SKILL.md"))
    missing = documented - _cli_commands()
    assert not missing, f"SKILL.md documents non-existent commands: {sorted(missing)}"


def test_skill_covers_key_commands():
    documented = _documented(os.path.join(ROOT, ".agents", "skills", "browser", "SKILL.md"))
    for expected in ("view", "tabs", "activate-tab", "mcp", "click", "diagnose", "replay"):
        assert expected in documented, f"SKILL.md missing key command: {expected}"


def test_readme_references_exist():
    text = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    for ref in ("docs/engineering/BREAKTHROUGH_ROADMAP.md",
                "docs/engineering/PROJECT_EVALUATION.md",
                "docs/engineering/AUTONOMOUS_PROGRESS.md",
                "fixtures.run_live_corpus", "fixtures.run_daemon_smoke"):
        assert ref in text
    for doc in ("BREAKTHROUGH_ROADMAP.md", "PROJECT_EVALUATION.md", "AUTONOMOUS_PROGRESS.md"):
        assert os.path.exists(os.path.join(ROOT, "docs", "engineering", doc))
