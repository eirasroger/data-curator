"""Load the extracted EPDs into a local DuckDB file as version 1 of each record.

The local counterpart of seed_bigquery.py, and the first thing to run for a
local setup. Every record lands as version 1 with no originating change request,
and as 'active' regardless of its date - so the nightly job discovers an expiry
and raises it through the pipeline, instead of the seed quietly asserting it.

Run:
    python scripts/seed_local.py               # refuses if rows already exist
    python scripts/seed_local.py --replace     # delete the file and reload
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from domain.store import get_store

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "seed" / "epds.json"
DEFAULT_DB = ROOT / "local.duckdb"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB), help="path to the DuckDB file")
    ap.add_argument("--replace", action="store_true",
                    help="delete the database file first instead of refusing")
    args = ap.parse_args()

    db = Path(args.db)
    if args.replace and db.exists():
        db.unlink()

    store = get_store("duckdb", path=str(db))

    count = store.epd_count()
    if count:
        print(f"epd_records already holds {count} rows. Use --replace to reload.")
        return 1

    records = json.loads(SEED.read_text(encoding="utf-8"))
    for rec in records:
        store.save_version(rec, rec["product_id"], 1, None, "active")

    print(f"loaded {len(records)} records into {db}")
    for row in store.query(
        "SELECT status, COUNT(*) AS records, MIN(expiry_date) AS earliest, "
        "MAX(expiry_date) AS latest FROM epd_current GROUP BY status ORDER BY status"
    ):
        print(f"  {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
