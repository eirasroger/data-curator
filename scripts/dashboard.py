"""Start the operator page locally and open it.

One command, no credentials, no cloud. Seeds the database if it is empty so a
first run lands on a page with something on it rather than an empty queue.

Run:
    python scripts/dashboard.py                      # the simulated history
    python scripts/dashboard.py --db local.duckdb    # whatever you have seeded
    python scripts/dashboard.py --no-browser
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sim_openai.duckdb")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    db = (ROOT / args.db) if not Path(args.db).is_absolute() else Path(args.db)
    if not db.exists():
        print(f"{db.name} does not exist. Create it with:")
        print(f"  python scripts/seed_local.py --db {db.name}")
        print(f"  python sim/run.py --db {db.name} --count 500")
        return 1

    env = {**os.environ, "STORE_BACKEND": "duckdb", "DUCKDB_PATH": str(db)}
    url = f"http://127.0.0.1:{args.port}"
    print(f"data-curator operator  ->  {url}   (ctrl-c to stop)")
    print(f"reading {db.name}")

    if not args.no_browser:
        # After a delay, so the browser does not beat uvicorn to the port.
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    return subprocess.call(
        [sys.executable, "-m", "uvicorn", "services.dashboard.main:app",
         "--host", "127.0.0.1", "--port", str(args.port)],
        cwd=str(ROOT), env=env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
