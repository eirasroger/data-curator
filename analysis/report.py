"""Regenerate every figure and number in FINDINGS.md.

One command, no manual steps, no hand-edited numbers. If a figure and the text
disagree, the text is stale and this is what fixes it.

The aggregation is SQL (analysis/models/decisions.sql) against the same tables
the worker writes, so it ports to BigQuery by re-quoting table names. This file
only draws.

Run:
    python analysis/report.py --db sim_openai.duckdb --drift-db sim_drift.duckdb
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from domain.store import get_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "analysis" / "models" / "decisions.sql"
FIGURES = ROOT / "analysis" / "figures"

# Slots 1 and 2 of the validated categorical palette, in fixed order, plus the
# status pair. Status is used only where the colour MEANS safe/unsafe.
BLUE, ORANGE = "#2a78d6", "#eb6834"
GOOD, CRITICAL = "#0ca30c", "#d03b3b"
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#8a8983"
SURFACE, GRID = "#fcfcfb", "#e6e5e1"

# The request-id tag the simulator stamps, mapped to what the proposal actually
# is. SHAPE_TRUTH is keyed by generator name; the stored rows carry the tag.
TAG_TRUTH = {
    "uncdec": "correct", "lifefix": "correct", "decfix": "correct",
    "comprep": "correct",
    "unitconf": "wrong", "unpatt": "wrong", "smalladj": "wrong",
    "baddens": "wrong", "badgwp": "wrong", "typeerr": "wrong",
    "lifespan": "ambiguous", "material": "ambiguous",
    "noop": "wrong", "badpath": "wrong",
    "replace": "ambiguous", "badreplace": "wrong",
}


def style(ax, title: str, xlabel: str = "", ylabel: str = "") -> None:
    """Recessive chrome: the data is the only thing with weight."""
    ax.set_title(title, color=INK, fontsize=12, pad=12, loc="left")
    ax.set_xlabel(xlabel, color=INK_2, fontsize=9)
    ax.set_ylabel(ylabel, color=INK_2, fontsize=9)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def figure(name: str, width: float = 7.2, height: float = 4.0):
    fig, ax = plt.subplots(figsize=(width, height), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    return fig, ax, FIGURES / name


def shown(path: Path) -> Path:
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def save(fig, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=160, facecolor=SURFACE)
    plt.close(fig)
    print(f"  wrote {shown(path)}")


def load(db: str):
    store = get_store("duckdb", path=db)
    store.conn.execute(MODELS.read_text(encoding="utf-8"))
    return store


# ---------------------------------------------------------------------------
# 1. Calibration: does a higher confidence mean a likelier-correct proposal?
# ---------------------------------------------------------------------------

def calibration(store, bins=(0.0, 0.5, 0.7, 0.8, 0.85, 0.9, 0.95, 1.01)) -> dict:
    rows = store.query(
        "SELECT confidence, triage, shape_tag FROM decisions WHERE reached_model"
    )
    labelled = [
        (float(r["confidence"]), TAG_TRUTH.get(r["shape_tag"]), r["triage"])
        for r in rows
        if TAG_TRUTH.get(r["shape_tag"]) in ("correct", "wrong")
    ]

    centres, rates, counts = [], [], []
    for lo, hi in zip(bins, bins[1:], strict=False):
        band = [x for x in labelled if lo <= x[0] < hi]
        if len(band) < 5:
            continue
        # Among proposals the triager is willing to accept (not implausible),
        # how many are actually correct? That is what the floor is betting on.
        offered = [x for x in band if x[2] != "implausible"]
        if not offered:
            continue
        centres.append((lo + min(hi, 1.0)) / 2)
        rates.append(sum(1 for x in offered if x[1] == "correct") / len(offered))
        counts.append(len(offered))

    fig, ax, path = figure("calibration.png")
    ax.plot([0, 1], [0, 1], color=MUTED, linewidth=1, linestyle=(0, (4, 3)),
            zorder=1, label="perfectly calibrated")
    ax.plot(centres, rates, color=BLUE, linewidth=2, marker="o", markersize=8,
            markeredgecolor=SURFACE, markeredgewidth=2, zorder=3,
            label="observed")
    for x, y, n in zip(centres, rates, counts, strict=True):
        ax.annotate(f"n={n}", (x, y), textcoords="offset points", xytext=(0, 11),
                    ha="center", fontsize=8, color=INK_2)
    # Both floors, because the point of the chart is that they differ: 0.85 is
    # what ships, 0.90 is where this data says the last wrong apply disappears.
    ax.axvline(0.85, color=CRITICAL, linewidth=1, linestyle=(0, (2, 2)), zorder=2)
    ax.annotate("0.85 shipped", (0.85, 0.44), textcoords="offset points",
                xytext=(-6, 0), fontsize=9, color=CRITICAL, ha="right")
    ax.axvline(0.90, color=GOOD, linewidth=1, linestyle=(0, (2, 2)), zorder=2)
    ax.annotate("0.90 safe", (0.90, 0.30), textcoords="offset points",
                xytext=(6, 0), fontsize=9, color=GOOD, ha="left")
    ax.set_xlim(0.3, 1.0)
    ax.set_ylim(0, 1.05)
    style(ax, "Stated confidence vs share actually correct",
          "stated confidence", "share correct")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="upper left")
    save(fig, path)
    return {"centres": centres, "rates": rates, "counts": counts}


# ---------------------------------------------------------------------------
# 2. Who settles what
# ---------------------------------------------------------------------------

def rules_vs_model(store) -> dict:
    rows = store.query(
        "SELECT settled_by, action, count(*) AS n FROM decisions GROUP BY 1,2"
    )
    actions = ["applied", "rejected", "pending_review"]
    rules = [next((r["n"] for r in rows
                   if r["settled_by"] == "rules" and r["action"] == a), 0)
             for a in actions]
    model = [next((r["n"] for r in rows
                   if r["settled_by"] == "model" and r["action"] == a), 0)
             for a in actions]

    fig, ax, path = figure("rules_vs_model.png", height=1.3 + 0.62 * 3)
    y = range(len(actions))
    height = 0.36
    ax.barh([v + height / 2 + 0.01 for v in y], rules, height=height,
            color=BLUE, label="settled by rules")
    ax.barh([v - height / 2 - 0.01 for v in y], model, height=height,
            color=ORANGE, label="reached the model")
    for i, (a, b) in enumerate(zip(rules, model, strict=True)):
        if a:
            ax.annotate(str(a), (a, i + height / 2 + 0.01), xytext=(5, 0),
                        textcoords="offset points", va="center", fontsize=9,
                        color=INK_2)
        if b:
            ax.annotate(str(b), (b, i - height / 2 - 0.01), xytext=(5, 0),
                        textcoords="offset points", va="center", fontsize=9,
                        color=INK_2)
    ax.set_yticks(list(y))
    ax.set_yticklabels([a.replace("_", " ") for a in actions])
    style(ax, "What decided each outcome", "decisions")
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="lower right")
    save(fig, path)

    total = sum(rules) + sum(model)
    return {"rules": sum(rules), "model": sum(model), "total": total,
            "by_action": dict(zip(actions,
                                  [r + m for r, m in zip(rules, model,
                                                         strict=True)],
                                  strict=True))}


# ---------------------------------------------------------------------------
# 3. What gets rejected, and by which check
# ---------------------------------------------------------------------------

def rejection_profile(store) -> dict:
    rows = store.query("SELECT * FROM rejection_reasons LIMIT 8")
    labels = [r["failing_field"][:38] for r in rows]
    values = [r["times"] for r in rows]

    fig, ax, path = figure("rejection_profile.png",
                           height=1.3 + 0.42 * max(len(labels), 3))
    ax.barh(range(len(labels)), values, color=BLUE, height=0.62)
    for i, v in enumerate(values):
        ax.annotate(str(v), (v, i), xytext=(5, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK_2)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    style(ax, "Which consistency check blocks a change", "times fired")
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    save(fig, path)
    return {r["failing_field"]: r["times"] for r in rows}


# ---------------------------------------------------------------------------
# 4. What a decision costs
# ---------------------------------------------------------------------------

def cost_and_latency(store) -> dict:
    rows = store.query(
        "SELECT cost_usd, latency_ms FROM decisions "
        "WHERE reached_model AND latency_ms IS NOT NULL"
    )
    latencies = sorted(int(r["latency_ms"]) for r in rows)
    costs = [float(r["cost_usd"] or 0) for r in rows]
    if not latencies:
        return {}

    fig, ax, path = figure("latency.png", height=3.4)
    ax.hist(latencies, bins=30, color=BLUE)
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)]
    for value, label, colour in ((p50, f"p50 {p50} ms", MUTED),
                                 (p95, f"p95 {p95} ms", CRITICAL)):
        ax.axvline(value, color=colour, linewidth=1.2, linestyle=(0, (3, 2)))
        ax.annotate(label, (value, ax.get_ylim()[1] * 0.92), xytext=(5, 0),
                    textcoords="offset points", fontsize=9, color=colour)
    style(ax, "How long one model call takes", "milliseconds", "decisions")
    save(fig, path)

    total = sum(costs)
    return {
        "calls": len(rows),
        "p50_ms": p50,
        "p95_ms": p95,
        "total_cost": total,
        "mean_cost": total / len(costs) if costs else 0.0,
    }


# ---------------------------------------------------------------------------
# 5. Drift
# ---------------------------------------------------------------------------

def drift(store, drift_store) -> dict:
    def series(s):
        rows = s.query(
            "SELECT day, rejection_rate, decisions FROM daily ORDER BY day"
        )
        return ([r["day"] for r in rows],
                [float(r["rejection_rate"]) for r in rows])

    days_a, rate_a = series(store)
    days_b, rate_b = series(drift_store)

    # Both runs use the same seed, so they are identical until the mix shifts.
    # Plotted as two full series one hides the other and the chart reads as
    # missing data. Draw the shared prefix once, then the two tails.
    split = 0
    for i, (da, db, ra, rb) in enumerate(zip(days_a, days_b, rate_a, rate_b,
                                             strict=False)):
        if da != db or abs(ra - rb) > 1e-9:
            split = i
            break

    fig, ax, path = figure("drift.png", height=3.8)
    ax.plot(days_a[:split + 1], rate_a[:split + 1], color=MUTED, linewidth=2,
            label="both runs (same proposals)")
    ax.plot(days_a[split:], rate_a[split:], color=BLUE, linewidth=2,
            label="stationary mix")
    ax.plot(days_b[split:], rate_b[split:], color=ORANGE, linewidth=2,
            label="shifted mix")
    ax.axhline(0.60, color=CRITICAL, linewidth=1.2, linestyle=(0, (3, 2)))
    ax.annotate("0.60 alert threshold", (days_a[0], 0.61), fontsize=9,
                color=CRITICAL, va="bottom")
    ax.set_ylim(0, 1.0)
    style(ax, "Daily rejection rate", "", "share rejected")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="upper left")
    fig.autofmt_xdate()
    save(fig, path)
    return {
        "stationary_mean": sum(rate_a) / len(rate_a),
        "shifted_mean": sum(rate_b) / len(rate_b),
        "stationary_max": max(rate_a),
        "shifted_max": max(rate_b),
    }


# ---------------------------------------------------------------------------
# 6. What each floor would do - the headline
# ---------------------------------------------------------------------------

def floors(store, candidates=(0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.60)) -> dict:
    rows = store.query(
        "SELECT confidence, triage, shape_tag, field_path FROM decisions "
        "WHERE reached_model"
    )
    cases = []
    for r in rows:
        truth = TAG_TRUTH.get(r["shape_tag"])
        if truth in ("correct", "wrong"):
            cases.append((float(r["confidence"]), r["triage"], truth,
                          r["field_path"]))

    from domain.changes import is_material

    table = []
    for floor in candidates:
        ok = bad = 0
        for confidence, triage, truth, field in cases:
            # Material fields never auto-apply whatever the score, so they are
            # not what a floor decides.
            if is_material(field or "") or triage == "implausible":
                continue
            if confidence < floor:
                continue
            if truth == "correct":
                ok += 1
            else:
                bad += 1
        table.append((floor, ok, bad))

    fig, ax, path = figure("floors.png", height=3.8)
    x = range(len(table))
    width = 0.38
    ax.bar([v - width / 2 - 0.01 for v in x], [t[1] for t in table], width,
           color=GOOD, label="correct, auto-applied")
    ax.bar([v + width / 2 + 0.01 for v in x], [t[2] for t in table], width,
           color=CRITICAL, label="WRONG, auto-applied")
    for i, (_, ok, bad) in enumerate(table):
        ax.annotate(str(ok), (i - width / 2 - 0.01, ok), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=9,
                    color=INK_2)
        if bad:
            ax.annotate(str(bad), (i + width / 2 + 0.01, bad), xytext=(0, 4),
                        textcoords="offset points", ha="center", fontsize=9,
                        color=CRITICAL)
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{t[0]:.2f}" for t in table])
    style(ax, "What each confidence floor would do", "confidence floor",
          "decisions")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="upper left")
    save(fig, path)

    safe = [t[0] for t in table if t[2] == 0 and t[1] > 0]
    return {"table": table, "lowest_safe_floor": min(safe) if safe else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sim_openai.duckdb",
                    help="the decision history to analyse")
    ap.add_argument("--drift-db", default="sim_drift.duckdb",
                    help="a history generated with --drift, for the drift chart")
    ap.add_argument("--out", default=str(ROOT / "analysis" / "summary.json"))
    ap.add_argument("--figures", default="", help="write figures here instead")
    args = ap.parse_args()

    global FIGURES
    if args.figures:
        FIGURES = Path(args.figures)
    FIGURES.mkdir(parents=True, exist_ok=True)
    store = load(args.db)
    drift_store = load(args.drift_db)

    print(f"analysing {args.db}")
    summary = {
        "database": args.db,
        "shapes": dict(Counter(
            r["shape_tag"] for r in store.query("SELECT shape_tag FROM decisions")
        )),
        "rules_vs_model": rules_vs_model(store),
        "calibration": calibration(store),
        "rejections": rejection_profile(store),
        "cost_latency": cost_and_latency(store),
        "drift": drift(store, drift_store),
        "floors": floors(store),
    }
    Path(args.out).write_text(json.dumps(summary, indent=2, default=str),
                              encoding="utf-8")
    print(f"  wrote {shown(Path(args.out))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
