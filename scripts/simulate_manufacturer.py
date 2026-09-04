"""Stand in for a manufacturer's publishing system.

Sends a genuine signed HTTP request to the deployed webhook endpoint. The only
invented part is that you wrote the sender; the wire format, the signature and
the receiver's verification are all real.

    python scripts/simulate_manufacturer.py 6
    python scripts/simulate_manufacturer.py 6 --bad-signature
    python scripts/simulate_manufacturer.py 6 --stale
    python scripts/simulate_manufacturer.py 6 --tamper

The secret is read from Secret Manager using your gcloud login, so it is not
stored locally.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from domain import webhooks  # noqa: E402

SEED = ROOT / "seed" / "epds.json"


def gcloud(*args: str) -> str:
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        raise SystemExit("gcloud not found on PATH")
    result = subprocess.run([exe, *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"gcloud {' '.join(args)} failed")
    return result.stdout.strip()


def build_new_version(record: dict) -> dict:
    """What a republished EPD would plausibly look like.

    A new validity date and slightly revised impacts, which is what a
    recalculation with updated background data produces.
    """
    updated = deepcopy(record)
    updated["date"] = "2031-06-30"
    impacts = updated.get("impacts") or {}
    for key in ("gwp_total", "gwp_fossil"):
        if isinstance(impacts.get(key), (int, float)):
            impacts[key] = round(impacts[key] * 0.94, 6)
    updated["impacts"] = impacts
    return updated


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("product_id", type=int)
    ap.add_argument("--source", default="manufacturer")
    ap.add_argument("--region", default="europe-west1")
    ap.add_argument("--url", default="", help="override the endpoint")
    ap.add_argument("--bad-signature", action="store_true",
                    help="sign with the wrong secret; expect 401")
    ap.add_argument("--stale", action="store_true",
                    help="sign with a timestamp an hour old; expect 401")
    ap.add_argument("--tamper", action="store_true",
                    help="sign one body then send a different one; expect 401")
    args = ap.parse_args()

    records = {r["product_id"]: r for r in json.loads(SEED.read_text(encoding="utf-8"))}
    if args.product_id not in records:
        raise SystemExit(f"no product {args.product_id} in the corpus")

    new_record = build_new_version(records[args.product_id])
    payload = {
        "event": "epd.republished",
        "epd_code": new_record.get("epd_code"),
        "reason": f"{args.source} republished {new_record.get('epd_code')} "
                  f"with revised A1-A3 figures",
        "record": new_record,
    }
    body = json.dumps(payload).encode()

    base = args.url or gcloud(
        "run", "services", "describe", "curator-webhook",
        f"--region={args.region}", "--format=value(status.url)")
    url = f"{base}/webhooks/{args.source}"

    secret = gcloud("secrets", "versions", "access", "latest",
                    f"--secret=webhook-secret-{args.source}")
    if args.bad_signature:
        secret = "not-the-right-secret"

    timestamp = str(int(time.time()) - (3600 if args.stale else 0))
    signature = webhooks.sign(secret, timestamp, body)

    if args.tamper:
        # Sign the original, transmit something else. This is what an attacker
        # who intercepted a valid request and edited it would produce.
        payload["record"]["impacts"]["gwp_total"] = 0.001
        body = json.dumps(payload).encode()

    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            webhooks.SIGNATURE_HEADER: signature,
            webhooks.TIMESTAMP_HEADER: timestamp,
        },
    )

    mode = ("bad signature" if args.bad_signature else
            "stale timestamp" if args.stale else
            "tampered body" if args.tamper else "correctly signed")
    print(f"POST {url}")
    print(f"  product {args.product_id}, {len(body)} bytes, {mode}")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"  HTTP {response.status}")
            print(f"  {response.read().decode()}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:200]
        print(f"  HTTP {exc.code}{'  ' + detail if detail else ''}")
        if exc.code == 401:
            print("  rejected, as expected" if mode != "correctly signed"
                  else "  rejected: check the secret matches the deployed one")
        return 0 if mode != "correctly signed" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
