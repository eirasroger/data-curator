#!/usr/bin/env bash
# The nightly reconciliation job. Run after scripts/deploy.sh.
#
# COST: Cloud Scheduler's free tier is 3 jobs per billing account. This is one.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/gcp_env.sh

JOB="${JOB:-curator-reconcile}"
SCHEDULE="${SCHEDULE:-0 2 * * *}"
TZ_NAME="${TZ_NAME:-Europe/Madrid}"
sa_email() { echo "$1@$PROJECT.iam.gserviceaccount.com"; }
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

WORKER_URL="$(gcloud run services describe curator-worker --region="$REGION" --format='value(status.url)')"

say "Letting the scheduler call the worker"
# The endpoint is not protected by a shared secret or an API key. Cloud Run IAM
# decides who may call it, and the scheduler has its own identity - so there is
# nothing to leak and nothing to rotate.
gcloud run services add-iam-policy-binding curator-worker \
  --region="$REGION" \
  --member="serviceAccount:$(sa_email curator-scheduler)" \
  --role="roles/run.invoker" --quiet >/dev/null
echo "  curator-scheduler -> run.invoker on curator-worker"

say "Scheduler job"
# --oidc-token-audience must be the service's base URL. Cloud Run checks the
# token's audience against itself, and a token minted for the wrong audience is
# rejected with a 401 that looks exactly like a permissions problem.
ARGS=(
  --location="$REGION"
  --schedule="$SCHEDULE"
  --time-zone="$TZ_NAME"
  --uri="$WORKER_URL/jobs/reconcile"
  --http-method=POST
  # A POST with no body is answered 411 Length Required by Google's front end
  # before it ever reaches Cloud Run. An empty body is enough to set
  # Content-Length. (--headers exists on `jobs create` but not `jobs update`,
  # so it is not used here - the endpoint ignores the body anyway.)
  --message-body={}
  --oidc-service-account-email="$(sa_email curator-scheduler)"
  --oidc-token-audience="$WORKER_URL"
  --attempt-deadline=600s
  --max-retry-attempts=3
)
if gcloud scheduler jobs describe "$JOB" --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$JOB" "${ARGS[@]}" --quiet >/dev/null
  echo "  updated $JOB"
else
  gcloud scheduler jobs create http "$JOB" "${ARGS[@]}" --quiet >/dev/null
  echo "  created $JOB"
fi
echo "  runs at '$SCHEDULE' ($TZ_NAME) -> POST $WORKER_URL/jobs/reconcile"

# Created paused. An unattended job that fires every night is a standing
# liability on a project nobody is watching, and every check it performs can be
# run on demand with `gcloud scheduler jobs run`. Unpause deliberately:
#   gcloud scheduler jobs resume curator-reconcile --location=$REGION
gcloud scheduler jobs pause "$JOB" --location="$REGION" --quiet >/dev/null 2>&1 || true
echo "  paused (will not fire on its own)"

say "Done"
echo "Run it now without waiting for 2am:"
echo "  gcloud scheduler jobs run $JOB --location=$REGION"
echo
echo "Then read the result:"
echo "  bq query --use_legacy_sql=false 'SELECT * FROM \`curator.reconciliation_runs\` ORDER BY run_at DESC LIMIT 1'"
