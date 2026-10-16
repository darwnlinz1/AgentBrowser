"""Run the benchmark harness: python -m benchmarks.run_benchmarks [--live]"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from benchmarks.harness import build_report, render_markdown


def main() -> None:
    live = "--live" in sys.argv
    report = build_report(live=live)
    out_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "latest.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    md = render_markdown(report)
    with open(os.path.join(out_dir, "latest.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    print(md)


if __name__ == "__main__":
    main()
