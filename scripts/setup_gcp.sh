#!/usr/bin/env bash
# One-time project setup. Idempotent: safe to re-run.
#
# Creates everything that does NOT depend on a deployed service URL. The push
# subscription and the Scheduler job need the worker's URL, so they live in
# scripts/deploy.sh instead.
#
# Cost: every resource here is free at rest. Pub/Sub topics, BigQuery tables,
# service accounts and secrets bill on use (messages, bytes scanned, secret
# accesses), and this project's volume is orders of magnitude inside free tier.
set -euo pipefail

PROJECT="${PROJECT:-data-curator-507614}"
REGION="${REGION:-europe-west1}"
BQ_LOCATION="${BQ_LOCATION:-EU}"
DATASET="${DATASET:-curator}"
TOPIC="${TOPIC:-hubspot-events}"
DLQ_TOPIC="${DLQ_TOPIC:-hubspot-events-dlq}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

gcloud config set project "$PROJECT" >/dev/null 2>&1

# --- Pub/Sub topics ---------------------------------------------------------
# Two topics, not one. The DLQ is where messages go after the worker has failed
# them max_delivery_attempts times. Without it, a message the worker can never
# process (bad schema, a bug) redelivers forever, burning LLM tokens on every
# attempt and hiding real failures behind endless retries.
say "Pub/Sub topics"
for t in "$TOPIC" "$DLQ_TOPIC"; do
  gcloud pubsub topics create "$t" 2>/dev/null && echo "created $t" || echo "exists  $t"
done

# --- BigQuery ---------------------------------------------------------------
say "BigQuery dataset ($BQ_LOCATION)"
bq --location="$BQ_LOCATION" mk --dataset \
   --description="Curated HubSpot golden record + agent registry" \
   "$PROJECT:$DATASET" 2>/dev/null && echo "created $DATASET" || echo "exists  $DATASET"

say "BigQuery tables and views"
for f in "$ROOT"/sql/0[123]_*.sql; do
  echo "  applying $(basename "$f")"
  bq --location="$BQ_LOCATION" query --project_id="$PROJECT" \
     --use_legacy_sql=false --quiet < "$f" >/dev/null
done

# --- Service accounts -------------------------------------------------------
# One identity per service, each with only the permissions that service needs.
# A single shared account would mean the public webhook endpoint also holds
# BigQuery write access - a compromise of the least-trusted component would
# hand over the datastore.
say "Service accounts"
declare -A SAS=(
  [curator-ingest]="Ingest service: publishes to Pub/Sub, nothing else"
  [curator-worker]="Enrichment worker: reads secrets, writes BigQuery"
  [curator-pubsub]="Pub/Sub push identity: invokes the worker"
  [curator-scheduler]="Cloud Scheduler identity: runs reconciliation"
)
for sa in "${!SAS[@]}"; do
  gcloud iam service-accounts create "$sa" --display-name="${SAS[$sa]}" 2>/dev/null \
    && echo "created $sa" || echo "exists  $sa"
done

sa_email() { echo "$1@$PROJECT.iam.gserviceaccount.com"; }

say "IAM bindings"
# ingest may publish to the events topic - and only that topic.
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
  --member="serviceAccount:$(sa_email curator-ingest)" \
  --role="roles/pubsub.publisher" --quiet >/dev/null
echo "  ingest    -> pubsub.publisher on $TOPIC"

# worker writes curated rows.
bq add-iam-policy-binding --project_id="$PROJECT" \
  --member="serviceAccount:$(sa_email curator-worker)" \
  --role="roles/bigquery.dataEditor" "$PROJECT:$DATASET" >/dev/null 2>&1 || true
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$(sa_email curator-worker)" \
  --role="roles/bigquery.jobUser" --condition=None --quiet >/dev/null
echo "  worker    -> bigquery.dataEditor on $DATASET, bigquery.jobUser on project"

# scheduler runs the reconciliation query and records the result.
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$(sa_email curator-scheduler)" \
  --role="roles/bigquery.jobUser" --condition=None --quiet >/dev/null
echo "  scheduler -> bigquery.jobUser"

# Dead-lettering is performed by Google's own Pub/Sub service agent, not by us,
# so that agent needs rights to publish into the DLQ and to ack the original.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
PUBSUB_AGENT="service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com"
gcloud pubsub topics add-iam-policy-binding "$DLQ_TOPIC" \
  --member="serviceAccount:$PUBSUB_AGENT" \
  --role="roles/pubsub.publisher" --quiet >/dev/null
echo "  pubsub agent -> pubsub.publisher on $DLQ_TOPIC"

# --- DLQ inspection subscription -------------------------------------------
# A topic with no subscription silently discards. Without this, dead-lettered
# messages would be dropped and the DLQ would be decorative.
say "DLQ inspection subscription"
gcloud pubsub subscriptions create "${DLQ_TOPIC}-sub" \
  --topic="$DLQ_TOPIC" --message-retention-duration=7d 2>/dev/null \
  && echo "created ${DLQ_TOPIC}-sub" || echo "exists  ${DLQ_TOPIC}-sub"

# --- Secrets ----------------------------------------------------------------
say "Secrets"
for s in openai-api-key hubspot-app-secret; do
  gcloud secrets create "$s" --replication-policy=automatic 2>/dev/null \
    && echo "created $s (empty - add a version before deploying)" || echo "exists  $s"
done
gcloud secrets add-iam-policy-binding openai-api-key \
  --member="serviceAccount:$(sa_email curator-worker)" \
  --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
gcloud secrets add-iam-policy-binding hubspot-app-secret \
  --member="serviceAccount:$(sa_email curator-ingest)" \
  --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
echo "  secret access granted to the one service that needs each"

say "Done. Next: add secret versions, then scripts/deploy.sh"
