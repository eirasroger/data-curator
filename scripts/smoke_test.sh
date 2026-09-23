#!/usr/bin/env bash
# Send real change requests to the deployed API and follow them through.
# Calls use an identity token from your gcloud login.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/gcp_env.sh

PY="${PY:-.venv/Scripts/python.exe}"
[ -x "$PY" ] || PY=python

API_URL="$(gcloud run services describe curator-api --region="$REGION" --format='value(status.url)')"
TOKEN="$(gcloud auth print-identity-token)"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

api() {
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$API_URL$path" \
      -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "$body"
  else
    curl -sS -X "$method" "$API_URL$path" -H "Authorization: Bearer $TOKEN"
  fi
}

submit() {
  api POST /changes "$1" | "$PY" scripts/_smoke_fmt.py request_id
}

wait_for() {
  # The API replies before the worker decides, so poll for the decision.
  local id="$1" action
  for _ in $(seq 1 30); do
    action="$(api GET "/changes/$id" 2>/dev/null | "$PY" scripts/_smoke_fmt.py action || true)"
    if [ -n "$action" ]; then
      api GET "/changes/$id" | "$PY" scripts/_smoke_fmt.py decision
      return 0
    fi
    sleep 2
  done
  echo "    -> no decision after 60s"
}

say "API health"
api GET /health; echo

say "1. A change the rules reject on their own"
# 95 kg/m3 x 0.1 m = 9.5 kg/m2, as declared, so 9.5 is rejected by rule.
echo "  product 6: density 95.0 -> 9.5"
ID1="$(submit '{
  "product_id": 6, "field_path": "density", "new_value": 9.5,
  "submitted_by": "smoke-test",
  "reason": "The datasheet says this is the weight per square metre."
}')"
echo "    request_id $ID1"
wait_for "$ID1"

say "2. A change that always needs a person"
# gwp_total is a published figure, so this goes to review.
echo "  product 6: impacts.gwp_total 11.0 -> 11.2"
ID2="$(submit '{
  "product_id": 6, "field_path": "impacts.gwp_total", "new_value": 11.2,
  "submitted_by": "smoke-test",
  "reason": "The published A1-A3 table gives 11.2 for the total."
}')"
echo "    request_id $ID2"
wait_for "$ID2"

say "3. A change nothing in the record can settle"
# Service life has no cross-check, so the model decides.
echo "  product 6: lifespan 50 -> 45"
ID3="$(submit '{
  "product_id": 6, "field_path": "lifespan", "new_value": 45.0,
  "submitted_by": "smoke-test",
  "reason": "I recall the declared service life being shorter."
}')"
echo "    request_id $ID3"
wait_for "$ID3"

say "4. The review queue"
api GET /reviews | "$PY" scripts/_smoke_fmt.py queue

say "5. A person approves the parked change"
api POST "/changes/$ID2/review" "{
  \"request_id\": \"$ID2\", \"reviewer\": \"roger\", \"approve\": true,
  \"note\": \"Checked against the PDF; 11.2 is the published figure.\"
}"; echo
sleep 10

say "6. The record afterwards"
api GET /epd/6 | "$PY" scripts/_smoke_fmt.py record

say "Done"
echo "The audit trail:"
echo "  bq query --use_legacy_sql=false 'SELECT request_id, field_path, action, actor, decision_reason FROM \`curator.change_status\` ORDER BY submitted_at DESC LIMIT 10'"
