"""Record the triage agent in the registry, with its latest eval scores.

An agent nobody owns and nobody has re-checked is a liability. This is what
keeps that visible: the registry row carries an owner, a pass rate, the number
of unsafe auto-applies, and when a human last looked.

    python eval/run_eval.py --provider openai --out /tmp/eval.json
    python scripts/register_agent.py --eval /tmp/eval.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT = os.environ.get("PROJECT", "data-curator-507614")
DATASET = os.environ.get("DATASET", "curator")
BQ_LOCATION = os.environ.get("BQ_LOCATION", "EU")
BQ = shutil.which("bq") or shutil.which("bq.cmd")

ap = argparse.ArgumentParser()
ap.add_argument("--eval", default="", help="JSON written by run_eval.py --out")
ap.add_argument("--owner", default="roger.verges.eiras@upc.edu")
args = ap.parse_args()

scores = json.loads(Path(args.eval).read_text(encoding="utf-8")) if args.eval else {}

row = {
    "agent_id": "epd-change-triage",
    "display_name": "EPD change triage",
    "purpose": (
        "Given a proposed change to an EPD field that passed every "
        "deterministic check, judge whether it is a correction or a mistake."
    ),
    "owner": args.owner,
    "status": "active",
    "provider": scores.get("provider") or os.environ.get("TRIAGE_PROVIDER", "openai"),
    "model": scores.get("model") or os.environ.get("TRIAGE_MODEL", "gpt-5-mini"),
    "prompt_version": "1",
    "input_contract": "EPD context block + the proposed field change (domain/triage.py build_context)",
    "output_contract": "TriageResult: triage class, confidence 0-1, rationale",
    "eval_pass_rate": scores.get("pass_rate"),
    "eval_unsafe": scores.get("unsafe"),
    "eval_size": scores.get("cases"),
    "eval_run_at": scores.get("run_at"),
    "last_reviewed": date.today().isoformat(),
    "created_at": datetime.now(timezone.utc).isoformat(),
}

# Replace rather than append: unlike the event log, the registry describes what
# is true NOW. History of an agent's scores lives in the eval runs, not here.
subprocess.run([BQ, f"--location={BQ_LOCATION}", "query", f"--project_id={PROJECT}",
                "--use_legacy_sql=false", "--quiet",
                f"DELETE FROM `{DATASET}.agent_registry` WHERE agent_id = 'epd-change-triage'"],
               capture_output=True, text=True)

with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False, encoding="utf-8") as fh:
    fh.write(json.dumps(row) + "\n")
    path = fh.name

result = subprocess.run(
    [BQ, f"--location={BQ_LOCATION}", "load", "--source_format=NEWLINE_DELIMITED_JSON",
     f"{PROJECT}:{DATASET}.agent_registry", path],
    capture_output=True, text=True)
os.unlink(path)
print(result.stdout.strip() or result.stderr.strip() or "registered epd-change-triage")
sys.exit(result.returncode)
