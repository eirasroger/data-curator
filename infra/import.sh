#!/usr/bin/env bash
# Import resources created before the Terraform config into state. Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")"

PROJECT="${PROJECT:-data-curator-507614}"
REGION="${REGION:-europe-west1}"
DATASET="${DATASET:-curator}"
TOPIC="${TOPIC:-epd-changes}"

TF="${TF:-terraform}"

# Terraform options must precede positional arguments.
adopt() {
  local address="$1" id="$2" out
  if "$TF" state show "$address" >/dev/null 2>&1; then
    echo "  have    $address"
    return
  fi
  if out=$("$TF" import -input=false -var "project=$PROJECT"              "$address" "$id" 2>&1); then
    echo "  adopted $address"
  else
    echo "  FAILED  $address"
    echo "$out" | sed -n '/^Error/,+3p' | sed 's/^/            /'
  fi
}

echo "Pub/Sub"
adopt 'google_pubsub_topic.changes' "projects/$PROJECT/topics/$TOPIC"
adopt 'google_pubsub_topic.dlq' "projects/$PROJECT/topics/$TOPIC-dlq"
adopt 'google_pubsub_subscription.dlq' "projects/$PROJECT/subscriptions/$TOPIC-dlq-sub"

echo "Service accounts"
for sa in api worker pubsub scheduler webhook dashboard; do
  adopt "google_service_account.sa[\"$sa\"]" \
    "projects/$PROJECT/serviceAccounts/curator-$sa@$PROJECT.iam.gserviceaccount.com"
done

echo "Secrets"
adopt 'google_secret_manager_secret.openai_api_key' \
  "projects/$PROJECT/secrets/openai-api-key"
adopt 'google_secret_manager_secret.webhook["manufacturer"]' \
  "projects/$PROJECT/secrets/webhook-secret-manufacturer"

echo "BigQuery"
adopt 'google_bigquery_dataset.curator' "projects/$PROJECT/datasets/$DATASET"
for t in epd_records change_requests change_events agent_registry reconciliation_runs; do
  adopt "google_bigquery_table.tables[\"$t\"]" "projects/$PROJECT/datasets/$DATASET/tables/$t"
done
for v in epd_current change_status agent_registry_attention; do
  adopt "google_bigquery_table.views[\"$v\"]" "projects/$PROJECT/datasets/$DATASET/tables/$v"
done
for v in epd_expiry_status review_queue; do
  adopt "google_bigquery_table.dependent_views[\"$v\"]" \
    "projects/$PROJECT/datasets/$DATASET/tables/$v"
done

echo
echo "IAM bindings are not imported: they are additive members rather than"
echo "objects, so applying them again is a no-op that costs nothing and"
echo "guarantees the grant exists."
echo
echo "Now:  terraform plan -var project=$PROJECT"
echo "A clean plan means the config matches what is deployed."
