"""Freeze the operator page into one self-contained HTML file for GitHub Pages.

The point of this file is what it does NOT do: it renders the same Jinja
templates the live service renders, from the same `metrics.overview()`. There is
no second implementation of the dashboard, so the public demo cannot drift away
from the thing it is demonstrating. The only difference is the `static` flag,
which swaps HTMX calls for browser-local ones and prints a banner saying so.

Writes docs/index.html, which is what GitHub Pages serves from the main branch.

Run:
    python analysis/export_dashboard.py --db sim_openai.duckdb
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from domain import metrics
from domain.store import get_store

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "services" / "dashboard" / "templates"
DOCS = ROOT / "docs"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sim_openai.duckdb",
                    help="the decision history to freeze")
    ap.add_argument("--out", default=str(DOCS / "index.html"))
    ap.add_argument("--queue-limit", type=int, default=40,
                    help="rows of the review queue to include in the snapshot")
    ap.add_argument("--with-figures", action="store_true",
                    help="also copy analysis/figures next to the page")
    args = ap.parse_args()

    store = get_store("duckdb", path=args.db)
    if store.epd_count() == 0:
        print(f"No EPDs in {args.db}. Seed and simulate first.")
        return 1

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )
    html = env.get_template("base.html").render(
        overview=metrics.overview(store, queue_limit=args.queue_limit),
        static=True,
        sla_days=metrics.REVIEW_SLA_DAYS,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    # GitHub Pages runs incoming files through Jekyll, which ignores paths
    # beginning with an underscore and would drop any partials added later.
    (out.parent / ".nojekyll").touch()
    print(f"wrote {out.relative_to(ROOT)} ({len(html) // 1024} KB)")

    if args.with_figures:
        target = out.parent / "figures"
        shutil.copytree(ROOT / "analysis" / "figures", target, dirs_exist_ok=True)
        print(f"wrote {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
