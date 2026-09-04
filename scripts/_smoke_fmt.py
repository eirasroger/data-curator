"""Formats JSON from stdin for smoke_test.sh.

A separate file rather than `python -c '...'` inside the shell script: nesting
quotes three deep (bash single quotes, Python string, JSON key) produces code
that looks fine and is a syntax error.
"""

import json
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "raw"
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    print("    (no JSON in response)")
    sys.exit(0)

if mode == "request_id":
    print(data.get("request_id", ""))

elif mode == "action":
    # Empty means the worker has not decided yet, so the caller keeps polling.
    print(data.get("action") or "")

elif mode == "decision":
    print(f"    -> {str(data.get('action', '?')).upper()}: {data.get('decision_reason', '')}")
    if data.get("triage"):
        cost = data.get("cost_usd") or 0
        print(f"       model said {data['triage']} @ {data.get('confidence')}  "
              f"(${float(cost):.5f}, {data.get('latency_ms')}ms)")
        rationale = (data.get("rationale") or "").strip()
        if rationale:
            print(f"       {rationale[:220]}")
    else:
        print("       settled by rules alone - no model call, no cost")

elif mode == "queue":
    print(f"  {data.get('count', 0)} change(s) waiting on a person")
    for item in data.get("items", [])[:6]:
        print(f"    {item['request_id'][:8]}  product {item['product_id']:<4} "
              f"{item.get('field_path')} -> {item.get('new_value')}")

elif mode == "record":
    impacts = data.get("impacts") or {}
    print(f"  product {data.get('product_id')} is at version {data.get('_version')}")
    print(f"    gwp_total = {impacts.get('gwp_total')}")
    print(f"    density   = {data.get('density')}")
    print(f"    status    = {data.get('status', 'active')}")

else:
    print(json.dumps(data, indent=2)[:800])
