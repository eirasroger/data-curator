#!/usr/bin/env bash
# One-time project setup. Idempotent - safe to re-run.
#
# Creates everything that does NOT depend on a deployed service URL. The push
# subscription and the Scheduler job need the worker's address, so they live in
# scripts/deploy.sh.
#
# COST: everything here is free at rest. Topics, tables, service accounts and
# secrets bill on use - messages published, bytes scanned, secret versions
# accessed - and this project's volume is far inside the free tier.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/gcp_env.sh

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
sa_email() { echo "$1@$PROJECT.iam.gserviceaccount.com"; }

gcloud config set project "$PROJECT" >/dev/null 2>&1

# --- Pub/Sub -----------------------------------------------------------------
# Two topics, not one.
#
# epd-changes carries change requests. epd-changes-dlq is where a message goes
# after the worker has failed it several times. Without a dead-letter topic, a
# message the worker can NEVER process - a bug, a shape it does not understand -
# redelivers forever: it burns a model call on every attempt, and it hides real
# failures inside an endless retry loop that looks like normal traffic.
say "Pub/Sub topics"
for t in "$TOPIC" "$DLQ_TOPIC"; do
  gcloud pubsub topics create "$t" 2>/dev/null && echo "  created $t" || echo "  exists  $t"
done

# A topic with no subscription silently discards everything published to it.
# Without this, the DLQ would be decorative and dead messages would vanish.
say "DLQ inspection subscription"
gcloud pubsub subscriptions create "${DLQ_TOPIC}-sub" \
  --topic="$DLQ_TOPIC" --message-retention-duration=7d 2>/dev/null \
  && echo "  created ${DLQ_TOPIC}-sub" || echo "  exists  ${DLQ_TOPIC}-sub"

# --- BigQuery ----------------------------------------------------------------
say "BigQuery dataset and schema"
bq --location="$BQ_LOCATION" mk --dataset \
   --description="EPD golden record, change log and agent registry" \
   "$PROJECT:$DATASET" 2>/dev/null && echo "  created $DATASET" || echo "  exists  $DATASET"
for f in sql/0*.sql; do
  printf "  applying %-26s " "$(basename "$f")"
  bq --location="$BQ_LOCATION" query --project_id="$PROJECT" \
     --use_legacy_sql=false --quiet < "$f" >/dev/null && echo "OK"
done

# --- Service accounts --------------------------------------------------------
# One identity per service, each holding only what that service needs.
#
# A single shared account would mean the public API - the least trusted thing
# here, since anyone can send it a request - also holds BigQuery write access.
# Splitting them means compromising the front door does not hand over the store.
say "Service accounts"
create_sa() {
  gcloud iam service-accounts create "$1" --display-name="$2" 2>/dev/null \
    && echo "  created $1" || echo "  exists  $1"
}
create_sa curator-api       "API: publishes change requests to Pub/Sub"
create_sa curator-worker    "Worker: reads secrets, writes BigQuery"
create_sa curator-pubsub    "Pub/Sub push identity: invokes the worker"
create_sa curator-scheduler "Cloud Scheduler identity: runs reconciliation"
create_sa curator-webhook   "Public webhook receiver: verifies and publishes"

say "IAM"
# The API may publish to the changes topic, and nothing else. It cannot read or
# write BigQuery at all - it never touches the store.
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
  --member="serviceAccount:$(sa_email curator-api)" \
  --role="roles/pubsub.publisher" --quiet >/dev/null
echo "  api       -> pubsub.publisher on $TOPIC"

# The worker publishes too - the nightly job raises expiry requests onto the
# same topic rather than editing records directly, which is what gives an
# expiry the same audit trail as any other change. Easy to forget, because the
# worker is otherwise a consumer; it shows up as a 403 the first time a record
# actually expires.
gcloud pubsub topics add-iam-policy-binding "$TOPIC"   --member="serviceAccount:$(sa_email curator-worker)"   --role="roles/pubsub.publisher" --quiet >/dev/null
echo "  worker    -> pubsub.publisher on $TOPIC (for expiry requests)"

# The worker is the ONLY writer to BigQuery. One writer means one place where
# the append-only rule can be broken, and one place to look when it is.
# Dataset-level access goes through the dataset's own access list. The
# obvious `bq add-iam-policy-binding` returns "This feature requires
# allowlisting" on an ordinary project, and tolerating that failure means the
# grant silently does not happen - you find out from a 403 in the logs later.
python scripts/grant_dataset_access.py
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$(sa_email curator-worker)" \
  --role="roles/bigquery.jobUser" --condition=None --quiet >/dev/null
echo "  worker    -> dataset WRITER + jobUser on project"

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$(sa_email curator-scheduler)" \
  --role="roles/bigquery.jobUser" --condition=None --quiet >/dev/null
echo "  scheduler -> bigquery.jobUser"

# Dead-lettering is carried out by Google's own Pub/Sub service agent, not by
# our code, so that agent needs rights to publish into the DLQ.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
PUBSUB_AGENT="service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com"
gcloud pubsub topics add-iam-policy-binding "$DLQ_TOPIC" \
  --member="serviceAccount:$PUBSUB_AGENT" \
  --role="roles/pubsub.publisher" --quiet >/dev/null
echo "  pubsub agent -> pubsub.publisher on $DLQ_TOPIC"

# --- Secrets -----------------------------------------------------------------
# The API key never appears in the image, the repo, or an environment variable
# we set by hand. Cloud Run reads it from Secret Manager at start-up using the
# worker's own identity, and only the worker is granted access.
# The webhook receiver is the only publicly reachable component, so it holds the
# least authority of anything here: publish to one topic, read its own signing
# secrets, nothing else. It cannot touch BigQuery.
gcloud pubsub topics add-iam-policy-binding "$TOPIC" --member="serviceAccount:$(sa_email curator-webhook)" --role="roles/pubsub.publisher" --quiet >/dev/null
echo "  webhook   -> pubsub.publisher on $TOPIC"

say "Secrets"
gcloud secrets create openai-api-key --replication-policy=automatic 2>/dev/null \
  && echo "  created openai-api-key (empty - add a version before deploying)" \
  || echo "  exists  openai-api-key"
gcloud secrets add-iam-policy-binding openai-api-key \
  --member="serviceAccount:$(sa_email curator-worker)" \
  --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
echo "  worker may read openai-api-key; nothing else may"

# One signing secret per webhook source. Separate secrets mean a leaked key
# compromises one sender, and revoking a sender is one secret version.
for src in manufacturer; do
  name="webhook-secret-$src"
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    echo "  exists  $name"
  else
    gcloud secrets create "$name" --replication-policy=automatic --quiet >/dev/null
    python -c "import secrets; print(secrets.token_hex(32), end='')" | gcloud secrets versions add "$name" --data-file=- >/dev/null
    echo "  created $name with a generated 256-bit key"
  fi
  gcloud secrets add-iam-policy-binding "$name" --member="serviceAccount:$(sa_email curator-webhook)" --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
done
echo "  webhook may read its signing secrets"

say "Done"
echo "Next:"
echo "  1. add the key:   gcloud secrets versions add openai-api-key --data-file=- <<< \"\$OPENAI_API_KEY\""
echo "  2. seed the data: python scripts/seed_bigquery.py"
echo "  3. deploy:        bash scripts/deploy.sh"
