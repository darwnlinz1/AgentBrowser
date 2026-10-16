"""Cycle 32 tests: source-map reconstruction + endpoint inventory."""

import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.source_reconstruct import (
    decode_data_url,
    normalize_source_path,
    parse_sourcemap,
    reconstruct_tree,
    write_tree,
)

MAP = {
    "version": 3,
    "file": "app.min.js",
    "sources": [
        "webpack:///./src/api/users.ts",
        "webpack:///./src/lib/util.ts",
        "../outside/escape.ts",
    ],
    "sourcesContent": [
        "export async function load(id) {\n  const r = await fetch(`/api/users/${id}`);\n  return r.json();\n}\n"
        "axios.post('/api/orders/42', {x: 1});\n",
        "export const x = 1;\nconst xhr = new XMLHttpRequest();\nxhr.open('GET', '/api/ping', true);\n",
        "evil",
    ],
    "names": ["load", "fetch"],
}


def test_normalize_paths_are_safe():
    assert normalize_source_path("webpack:///./src/api/users.ts") == "src/api/users.ts"
    assert normalize_source_path("../outside/escape.ts") == "outside/escape.ts"
    assert not normalize_source_path("/etc/passwd").startswith("/")


def test_reconstruct_tree_and_endpoints():
    tree = reconstruct_tree([parse_sourcemap(MAP, "https://app.example/app.min.js")])
    assert tree["files_count"] == 3
    assert tree["with_content"] == 3
    paths = {e["path"] for e in tree["endpoints"]}
    assert "/api/users/{var}" in paths
    assert "/api/orders/{n}" in paths
    assert "/api/ping" in paths
    methods = {e["method"] for e in tree["endpoints"]}
    assert "POST" in methods and "GET" in methods


def test_data_url_map_roundtrip():
    payload = base64.b64encode(json.dumps(MAP).encode()).decode()
    decoded = decode_data_url(f"data:application/json;charset=utf-8;base64,{payload}")
    assert decoded["file"] == "app.min.js"
    assert decode_data_url("https://x/app.js.map") is None


def test_write_tree_confined(tmp_path):
    tree = reconstruct_tree([parse_sourcemap(MAP)])
    out = write_tree(tree, str(tmp_path / "tree"))
    assert out["count"] == 3
    written = {w["path"] for w in out["written"]}
    assert "src/api/users.ts" in written
    # The escape attempt lands inside the output root, never outside it.
    for w in out["written"]:
        assert os.path.abspath(w["file"]).startswith(os.path.abspath(str(tmp_path)))
    assert (tmp_path / "tree" / "src" / "api" / "users.ts").exists()
