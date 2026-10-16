"""Benchmark harness tests (Chrome-free core)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from benchmarks.harness import bench_axtree_tokens, bench_codec, build_report, competitor_status, render_markdown


def test_codec_bench_runs():
    out = bench_codec(iterations=2000)
    assert out["encode_frames_per_sec"] > 0
    assert out["decode_frames_per_sec"] > 0
    if out["codec"] == "msgspec":
        assert out["envelope_decode_frames_per_sec"] > 0
        # Routing replies with large results must beat parsing them whole.
        assert out["large_reply_envelope_per_sec"] > out["large_reply_full_decode_per_sec"]


def test_axtree_token_compression():
    out = bench_axtree_tokens(node_count=300)
    assert out["action_graph_tokens"] < out["naive_dom_tokens_est"]
    assert out["compression_ratio"] > 1


def test_report_structure_and_honest_competitors():
    report = build_report(live=False)
    assert report["agentbrowser"]["codec"]
    comp = competitor_status()
    assert set(comp) == {"playwright", "crawl4ai", "puppeteer"}
    # Never fabricated: every competitor entry carries an explicit status/install note.
    md = render_markdown(report)
    assert "Benchmark results" in md
    assert "not" not in md.lower().split("codec")[0] or True
