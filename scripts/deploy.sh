#!/usr/bin/env bash
# Build both images, deploy both services, and wire Pub/Sub to the worker.
# Idempotent - safe to re-run. Run scripts/setup_gcp.sh first.
#
# COST
#   Cloud Build   free tier 2,500 build-minutes/month; each build here is ~1-2.
#   Artifact Reg. free tier 0.5 GB. Two images at ~250 MB would exceed that
#                 after a few revisions, so a cleanup policy below keeps only
#                 the newest 3 of each. Without it this is the one line item
#                 that would quietly start costing pennies.
#   Cloud Run     free tier 2M requests + 180k vCPU-seconds/month. Both services
#                 scale to zero, so an idle deployment costs nothing at all.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/gcp_env.sh

REPO="${REPO:-curator}"
REGISTRY="$REGION-docker.pkg.dev/$PROJECT/$REPO"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
sa_email() { echo "$1@$PROJECT.iam.gserviceaccount.com"; }

# --- Artifact Registry -------------------------------------------------------
say "Artifact Registry"
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION" \
  --description="data-curator service images" 2>/dev/null \
  && echo "  created $REPO" || echo "  exists  $REPO"

# Keep only the newest few images. Every deploy pushes a new one, and old ones
# are never read again - they just accumulate against the free tier.
cat > /tmp/cleanup-policy.json <<'JSON'
[
  {
    "name": "keep-newest-3",
    "action": {"type": "Keep"},
    "mostRecentVersions": {"keepCount": 3}
  },
  {
    "name": "delete-the-rest",
    "action": {"type": "Delete"},
    "condition": {"olderThan": "7d"}
  }
]
JSON
gcloud artifacts repositories set-cleanup-policies "$REPO" \
  --location="$REGION" --policy=/tmp/cleanup-policy.json --quiet >/dev/null 2>&1 \
  && echo "  cleanup policy: keep newest 3" || echo "  cleanup policy: skipped"

# --- Build -------------------------------------------------------------------
say "Building images"
for svc in api worker webhook; do
  echo "  building $svc ..."
  gcloud builds submit --config=cloudbuild.yaml \
    --substitutions="_SERVICE=$svc,_IMAGE=$REGISTRY/$svc:latest" \
    --quiet . >/dev/null
  echo "  built $REGISTRY/$svc:latest"
done

# --- Deploy ------------------------------------------------------------------
# --max-instances is a spending guard, not a performance setting. Without it a
# retry storm or a runaway loop can scale to hundreds of instances, each one
# making model calls. Three is far more than this needs.
#
# --no-allow-unauthenticated: neither service is open to the internet. The API
# is called with an identity token; the worker only by Pub/Sub.
say "Deploying api"
gcloud run deploy curator-api \
  --image="$REGISTRY/api:latest" \
  --region="$REGION" \
  --service-account="$(sa_email curator-api)" \
  --set-env-vars="GCP_PROJECT=$PROJECT,PUBSUB_TOPIC=$TOPIC,BQ_DATASET=$DATASET" \
  --no-allow-unauthenticated \
  --max-instances=3 --memory=512Mi --timeout=60s \
  --quiet >/dev/null
API_URL="$(gcloud run services describe curator-api --region="$REGION" --format='value(status.url)')"
echo "  $API_URL"

say "Deploying webhook receiver"
# --allow-unauthenticated, and it is the only service with that flag.
#
# A webhook sender is an external system with no Google identity, so IAM cannot
# gate this endpoint. The HMAC signature is the access control, which is why
# this service is separate: Cloud Run authentication applies per service and not
# per path, so hosting the webhook on curator-api would expose /changes,
# /reviews and /epd to the internet as well.
gcloud run deploy curator-webhook --image="$REGISTRY/webhook:latest" --region="$REGION" --service-account="$(sa_email curator-webhook)" --set-env-vars="GCP_PROJECT=$PROJECT,PUBSUB_TOPIC=$TOPIC" --set-secrets="WEBHOOK_SECRET_MANUFACTURER=webhook-secret-manufacturer:latest" --allow-unauthenticated --max-instances=3 --memory=512Mi --timeout=30s --quiet >/dev/null
WEBHOOK_URL="$(gcloud run services describe curator-webhook --region="$REGION" --format='value(status.url)')"
echo "  $WEBHOOK_URL  (public)"

say "Deploying worker"
# The key is mounted from Secret Manager at start-up, using the worker's own
# identity. It is never in the image, the repo, or a --set-env-vars flag, and
# rotating it means adding a secret version, not rebuilding anything.
gcloud run deploy curator-worker \
  --image="$REGISTRY/worker:latest" \
  --region="$REGION" \
  --service-account="$(sa_email curator-worker)" \
  --set-env-vars="GCP_PROJECT=$PROJECT,BQ_DATASET=$DATASET,TRIAGE_PROVIDER=${TRIAGE_PROVIDER:-openai},TRIAGE_MODEL=${TRIAGE_MODEL:-gpt-5-mini}" \
  --set-secrets="OPENAI_API_KEY=openai-api-key:latest" \
  --no-allow-unauthenticated \
  --max-instances=3 --memory=512Mi --timeout=120s \
  --quiet >/dev/null
WORKER_URL="$(gcloud run services describe curator-worker --region="$REGION" --format='value(status.url)')"
echo "  $WORKER_URL"

# --- Permissions that need a deployed service --------------------------------
say "IAM for the deployed services"
# Pub/Sub does not get to call the worker just because it knows the URL. It
# signs each push with an OIDC token for this identity, and Cloud Run checks it.
gcloud run services add-iam-policy-binding curator-worker \
  --region="$REGION" \
  --member="serviceAccount:$(sa_email curator-pubsub)" \
  --role="roles/run.invoker" --quiet >/dev/null
echo "  curator-pubsub -> run.invoker on curator-worker"

# The API reads the review queue and current records. Read only: it must never
# be able to write to the store.
python scripts/grant_dataset_access.py >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$(sa_email curator-api)" \
  --role="roles/bigquery.jobUser" --condition=None --quiet >/dev/null
echo "  curator-api -> dataset READER (read only) + jobUser"

# --- The push subscription ---------------------------------------------------
# --ack-deadline=60, not the 10s default. The eval measured the model at 4.6s
# median and 7.7s at p95, and BigQuery writes come after that. At 10s Pub/Sub
# would start redelivering messages the worker was still successfully
# processing - which looks like random duplicate work and is miserable to debug.
#
# --max-delivery-attempts=5 then dead-letter: a message that has failed five
# times is not going to succeed on the sixth, and each attempt costs a model
# call.
say "Pub/Sub push subscription"
PUSH_ARGS=(
  --topic="$TOPIC"
  --push-endpoint="$WORKER_URL/"
  --push-auth-service-account="$(sa_email curator-pubsub)"
  --ack-deadline=60
  --dead-letter-topic="$DLQ_TOPIC"
  --max-delivery-attempts=5
  --min-retry-delay=10s
  --max-retry-delay=600s
  --message-retention-duration=7d
)
if gcloud pubsub subscriptions describe "$SUBSCRIPTION" >/dev/null 2>&1; then
  gcloud pubsub subscriptions update "$SUBSCRIPTION" \
    --push-endpoint="$WORKER_URL/" \
    --push-auth-service-account="$(sa_email curator-pubsub)" \
    --ack-deadline=60 \
    --dead-letter-topic="$DLQ_TOPIC" --max-delivery-attempts=5 \
    --quiet >/dev/null
  echo "  updated $SUBSCRIPTION"
else
  gcloud pubsub subscriptions create "$SUBSCRIPTION" "${PUSH_ARGS[@]}" --quiet >/dev/null
  echo "  created $SUBSCRIPTION"
fi

# Dead-lettering is done by Google's Pub/Sub service agent, which needs to be
# able to acknowledge the original message as well as publish the copy.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
gcloud pubsub subscriptions add-iam-policy-binding "$SUBSCRIPTION" \
  --member="serviceAccount:service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role="roles/pubsub.subscriber" --quiet >/dev/null
echo "  pubsub agent -> pubsub.subscriber on $SUBSCRIPTION"

say "Deployed"
echo "  API     $API_URL"
echo "  worker  $WORKER_URL"
echo "  webhook $WEBHOOK_URL  (public, signature-verified)"
echo
echo "Try it:"
echo "  TOKEN=\$(gcloud auth print-identity-token)"
echo "  curl -H \"Authorization: Bearer \$TOKEN\" $API_URL/health"
