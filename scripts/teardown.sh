#!/usr/bin/env bash
# Remove the services, scheduler and push subscription; keep data and secrets.
# Usage: bash scripts/teardown.sh [--images] [--data]
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/gcp_env.sh
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Cloud Run services"
for svc in curator-api curator-worker curator-webhook; do
  if gcloud run services describe "$svc" --region="$REGION" >/dev/null 2>&1; then
    gcloud run services delete "$svc" --region="$REGION" --quiet >/dev/null
    echo "  deleted $svc"
  else
    echo "  absent  $svc"
  fi
done

say "Scheduler"
gcloud scheduler jobs delete curator-reconcile --location="$REGION" --quiet >/dev/null 2>&1 \
  && echo "  deleted curator-reconcile" || echo "  absent  curator-reconcile"

# The subscription would otherwise push to a deleted service.
say "Push subscription"
gcloud pubsub subscriptions delete "$SUBSCRIPTION" --quiet >/dev/null 2>&1 \
  && echo "  deleted $SUBSCRIPTION" || echo "  absent  $SUBSCRIPTION"

if [[ "${1:-}" == "--images" || "${2:-}" == "--images" ]]; then
  say "Images"
  gcloud artifacts repositories delete curator --location="$REGION" --quiet >/dev/null 2>&1 \
    && echo "  deleted the curator repository" || echo "  absent"
fi

if [[ "${1:-}" == "--data" || "${2:-}" == "--data" ]]; then
  say "BigQuery"
  read -r -p "  Delete dataset $DATASET and all records? type yes: " confirm
  if [[ "$confirm" == "yes" ]]; then
    bq rm -r -f -d "$PROJECT:$DATASET" && echo "  deleted $DATASET"
  else
    echo "  kept"
  fi
fi

say "Done"
echo "Kept: BigQuery data, Pub/Sub topics, secrets, service accounts."
echo "Restore with: bash scripts/deploy.sh"
