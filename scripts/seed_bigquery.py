"""Load the extracted EPDs into BigQuery as version 1 of each record.

Writes newline-delimited JSON to a temp file, then hands it to `bq load`. That
is the plain way to get a file into BigQuery, and it means this script needs no
Python client library - the whole thing is the CLI plus a shape conversion.

Every record lands as version 1 with change_request_id NULL, which is what
"nobody has changed this yet" looks like. Later versions are written by the
worker when a change is applied.

Run:
    python scripts/seed_bigquery.py            # refuses if rows already exist
    python scripts/seed_bigquery.py --replace  # wipe and reload
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "seed" / "epds.json"

PROJECT = os.environ.get("PROJECT", "data-curator-507614")
DATASET = os.environ.get("DATASET", "curator")
BQ_LOCATION = os.environ.get("BQ_LOCATION", "EU")
TABLE = f"{PROJECT}:{DATASET}.epd_records"


def _bq_executable() -> str:
    """Find the bq CLI.

    On Windows it is bq.cmd, and subprocess will not find that from a bare "bq"
    the way a shell would - PATHEXT resolution is the shell's job, not
    CreateProcess's. shutil.which does apply PATHEXT, so it finds the .cmd.
    """
    import shutil

    for name in ("bq", "bq.cmd"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit(
        "bq CLI not found on PATH. Install the Google Cloud SDK, or run "
        ". scripts/gcp_env.sh first."
    )


BQ = _bq_executable()


def bq(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BQ, f"--location={BQ_LOCATION}", *args],
        capture_output=capture, text=True, check=False, shell=False,
    )


def to_row(rec: dict, now: str) -> dict:
    """One EPD record, flattened into the columns worth querying directly."""
    impacts = rec.get("impacts") or {}

    expiry = rec.get("date")
    try:
        date.fromisoformat(str(expiry))  # validate; the value is stored as-is
    except ValueError:
        expiry = None

    return {
        "product_id": rec["product_id"],
        "version": 1,
        "valid_from": now,
        "change_request_id": None,
        "epd_code": rec.get("epd_code"),
        "prod_name": rec.get("prod_name"),
        "prod_man": rec.get("prod_man"),
        "prod_site": rec.get("prod_site"),
        "expiry_date": expiry,
        # Always 'active', even for a record whose date has already passed.
        #
        # Stamping 'expired' here looked tidier and was wrong: it produced a
        # record marked expired with no change request behind it, no decision,
        # and no event saying when anyone noticed. On compliance data the fact
        # that something expired IS an event, and it has to be in the log.
        #
        # Loading everything active lets the nightly job discover the lapse and
        # raise it through the normal pipeline, which leaves the audit trail.
        "status": "active",
        "reference_unit": rec.get("reference_unit"),
        "density": rec.get("density"),
        "thickness": rec.get("thickness"),
        "lifespan": rec.get("lifespan"),
        "gwp_total": impacts.get("gwp_total"),
        "gwp_fossil": impacts.get("gwp_fossil"),
        "gwp_luluc": impacts.get("gwp_luluc"),
        "gwp_bio": impacts.get("gwp_bio"),
        # A JSON column loaded from NDJSON takes the value as a JSON string.
        "record": json.dumps(rec, ensure_ascii=False),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replace", action="store_true",
                    help="truncate the table first instead of refusing")
    args = ap.parse_args()

    existing = bq("query", f"--project_id={PROJECT}", "--use_legacy_sql=false",
                  "--format=csv", "--quiet",
                  f"SELECT COUNT(*) FROM `{DATASET}.epd_records`", capture=True)
    count = 0
    if existing.returncode == 0:
        lines = [ln for ln in existing.stdout.strip().splitlines() if ln.strip()]
        if len(lines) > 1:
            count = int(lines[-1])

    if count and not args.replace:
        print(f"epd_records already holds {count} rows. Use --replace to reload.")
        return 1

    records = json.loads(SEED.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).isoformat()
    rows = [to_row(r, now) for r in records]

    with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False,
                                     encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        path = fh.name

    load_args = ["load", "--source_format=NEWLINE_DELIMITED_JSON"]
    if args.replace:
        load_args.append("--replace")
    load_args += [TABLE, path]

    print(f"loading {len(rows)} records into {TABLE} ...")
    result = bq(*load_args)
    os.unlink(path)
    if result.returncode != 0:
        return result.returncode

    check = bq("query", f"--project_id={PROJECT}", "--use_legacy_sql=false",
               "--format=pretty",
               # One line on purpose: bq is unreliable about a query argument
               # that carries embedded newlines and indentation.
               f"SELECT status, COUNT(*) AS records, MIN(expiry_date) AS earliest, "
               f"MAX(expiry_date) AS latest FROM `{DATASET}.epd_current` "
               f"GROUP BY status ORDER BY status")
    return check.returncode


if __name__ == "__main__":
    raise SystemExit(main())
