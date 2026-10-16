"""Cycle 19 tests: standalone Python/Node reproduction synthesizers."""

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.synth import build_node_repro, build_python_repro

EVIL = 'http://x/"); import os; os.system("calc'
TARGET = "#go'); drop_tables('"


def test_python_repro_compiles_and_keeps_payload_inert():
    script = build_python_repro(EVIL, TARGET, expected_exception="TypeError", expected_status=500)
    compile(script, "<repro>", "exec")
    # The artifact must find the package outside the repo too.
    assert "AGENTBROWSER_HOME" in script
    # And it must not collide with a running browser on the default port/profile.
    assert "find_free_port" in script and "ab-repro" in script
    assert 'os.system("calc' not in script.split("SPEC = json.loads(")[0]
    assert "SPEC = json.loads(" in script
    assert "--expect" in script


def test_node_repro_syntax_and_spec(tmp_path):
    script = build_node_repro("http://127.0.0.1:9/case", "#go",
                              expected_exception="Cannot read properties", expected_status=500)
    assert "__SPEC_LITERAL__" not in script
    assert "remote-debugging-pipe" in script
    node = shutil.which("node")
    if not node:
        return
    path = tmp_path / "repro.mjs"
    # Template uses CommonJS require; check as .cjs semantics via --check on .js copy.
    js_path = tmp_path / "repro.js"
    js_path.write_text(script, encoding="utf-8")
    proc = subprocess.run([node, "--check", str(js_path)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
