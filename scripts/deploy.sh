#!/usr/bin/env bash
# Build and deploy the services and connect Pub/Sub to the worker. Safe to re-run.
# Run `terraform -chdir=infra apply` first.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/gcp_env.sh

REPO="${REPO:-curator}"
REGISTRY="$REGION-docker.pkg.dev/$PROJECT/$REPO"

# The public webhook is opt-in: pass --with-webhook, or use scripts/webhook_demo.sh.
WITH_WEBHOOK=0
for arg in "$@"; do
  case "$arg" in
    --with-webhook) WITH_WEBHOOK=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
sa_email() { echo "$1@$PROJECT.iam.gserviceaccount.com"; }

# --- Artifact Registry -------------------------------------------------------
say "Artifact Registry"
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION" \
  --description="data-curator service images" 2>/dev/null \
  && echo "  created $REPO" || echo "  exists  $REPO"

# Keep the newest 3 images to stay within the free storage tier.
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
services_to_build="api worker"
if [ "$WITH_WEBHOOK" = 1 ]; then
  services_to_build="$services_to_build webhook"
fi
for svc in $services_to_build; do
  echo "  building $svc ..."
  gcloud builds submit --config=cloudbuild.yaml \
    --substitutions="_SERVICE=$svc,_IMAGE=$REGISTRY/$svc:latest" \
    --quiet . >/dev/null
  echo "  built $REGISTRY/$svc:latest"
done

# --- Deploy ------------------------------------------------------------------
# --max-instances caps spending. Both services require a Google identity.
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

if [ "$WITH_WEBHOOK" = 1 ]; then
say "Deploying webhook receiver"
# The only public service; the HMAC signature is its access control.
gcloud run deploy curator-webhook --image="$REGISTRY/webhook:latest" --region="$REGION" --service-account="$(sa_email curator-webhook)" --set-env-vars="GCP_PROJECT=$PROJECT,PUBSUB_TOPIC=$TOPIC" --set-secrets="WEBHOOK_SECRET_MANUFACTURER=webhook-secret-manufacturer:latest" --allow-unauthenticated --max-instances=3 --memory=512Mi --timeout=30s --quiet >/dev/null
WEBHOOK_URL="$(gcloud run services describe curator-webhook --region="$REGION" --format='value(status.url)')"
echo "  $WEBHOOK_URL  (public)"
else
WEBHOOK_URL=""
say "Skipping webhook receiver (--with-webhook to deploy it)"
fi

say "Deploying worker"
# The API key is mounted from Secret Manager; rotate it by adding a version.
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
# Pub/Sub pushes with an OIDC token for this identity.
gcloud run services add-iam-policy-binding curator-worker \
  --region="$REGION" \
  --member="serviceAccount:$(sa_email curator-pubsub)" \
  --role="roles/run.invoker" --quiet >/dev/null
echo "  curator-pubsub -> run.invoker on curator-worker"

# BigQuery access is granted in infra/bigquery.tf and infra/iam.tf.

# --- The push subscription ---------------------------------------------------
# 60s ack deadline covers a slow model call (p95 about 8s) plus writes.
# After 5 failed attempts a message goes to the dead-letter topic.
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

# The Pub/Sub service agent needs subscriber rights to dead-letter messages.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
gcloud pubsub subscriptions add-iam-policy-binding "$SUBSCRIPTION" \
  --member="serviceAccount:service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role="roles/pubsub.subscriber" --quiet >/dev/null
echo "  pubsub agent -> pubsub.subscriber on $SUBSCRIPTION"

say "Deployed"
echo "  API     $API_URL"
echo "  worker  $WORKER_URL"
if [ -n "$WEBHOOK_URL" ]; then
  echo "  webhook $WEBHOOK_URL  (public, signature-verified)"
else
  echo "  webhook not deployed"
fi
echo
echo "Try it:"
echo "  TOKEN=\$(gcloud auth print-identity-token)"
echo "  curl -H \"Authorization: Bearer \$TOKEN\" $API_URL/health"
