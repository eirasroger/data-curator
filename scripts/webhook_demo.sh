#!/usr/bin/env bash
# Stand up the public webhook receiver, exercise it, tear it back down.
#
# The receiver has to be reachable without a Google identity, so it is the only
# component a stranger can call. Google Cloud has no hard spending cap, so the
# only real control over an endpoint like this is for it not to exist when
# nobody is using it. This script keeps its lifetime to a couple of minutes.
#
#   bash scripts/webhook_demo.sh        deploy, demo, delete
#   bash scripts/webhook_demo.sh --keep deploy and demo, leave it running
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/gcp_env.sh

PY="${PY:-.venv/Scripts/python.exe}"
[ -x "$PY" ] || PY=python
REGISTRY="$REGION-docker.pkg.dev/$PROJECT/${REPO:-curator}"
KEEP="${1:-}"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

cleanup() {
  if [[ "$KEEP" != "--keep" ]]; then
    say "Removing the public endpoint"
    gcloud run services delete curator-webhook --region="$REGION" --quiet >/dev/null 2>&1 \
      && echo "  deleted curator-webhook" || echo "  already gone"
    echo "  no publicly reachable service remains"
  else
    say "Left running"
    gcloud run services describe curator-webhook --region="$REGION" --format='value(status.url)'
    echo "  delete it with: gcloud run services delete curator-webhook --region=$REGION"
  fi
}
trap cleanup EXIT

say "Deploying the receiver"
# --max-instances=1: one instance is ample for a demonstration and caps what a
# flood could cost while the endpoint is up.
gcloud run deploy curator-webhook --image="$REGISTRY/webhook:latest" --region="$REGION" --service-account="curator-webhook@$PROJECT.iam.gserviceaccount.com" --set-env-vars="GCP_PROJECT=$PROJECT,PUBSUB_TOPIC=$TOPIC" --set-secrets="WEBHOOK_SECRET_MANUFACTURER=webhook-secret-manufacturer:latest" --allow-unauthenticated --max-instances=1 --memory=512Mi --timeout=30s --quiet >/dev/null
URL="$(gcloud run services describe curator-webhook --region="$REGION" --format='value(status.url)')"
echo "  $URL"

say "1. Correctly signed"
"$PY" scripts/simulate_manufacturer.py "${PRODUCT_ID:-6}" | tail -3

say "2. Wrong secret"
"$PY" scripts/simulate_manufacturer.py "${PRODUCT_ID:-6}" --bad-signature | tail -2

say "3. Replayed an hour later"
"$PY" scripts/simulate_manufacturer.py "${PRODUCT_ID:-6}" --stale | tail -2

say "4. Body altered after signing"
"$PY" scripts/simulate_manufacturer.py "${PRODUCT_ID:-6}" --tamper | tail -2

say "Where the accepted one went"
sleep 12
bq query --project_id="$PROJECT" --use_legacy_sql=false --format=pretty --quiet \
  'SELECT SUBSTR(request_id,1,8) AS req, submitted_by, action, decision_reason FROM `curator.change_status` WHERE kind="record_replacement" ORDER BY submitted_at DESC LIMIT 1' 2>&1 | tail -6
