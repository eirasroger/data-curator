# One identity per service, each holding only what that service needs.
#
# A single shared account would mean the public webhook receiver - the least
# trusted thing here, since anyone can send it a request - also held BigQuery
# write access. Splitting them means compromising the front door does not hand
# over the store.

locals {
  service_accounts = {
    api       = "API: publishes change requests to Pub/Sub"
    worker    = "Worker: reads secrets, writes BigQuery"
    pubsub    = "Pub/Sub push identity: invokes the worker"
    scheduler = "Cloud Scheduler identity: runs reconciliation"
    webhook   = "Public webhook receiver: verifies and publishes"
    dashboard = "Operator dashboard: reads BigQuery, publishes reviews"
  }
}

resource "google_service_account" "sa" {
  for_each     = local.service_accounts
  account_id   = "curator-${each.key}"
  display_name = each.value
}

locals {
  sa_member = {
    for k, v in google_service_account.sa : k => "serviceAccount:${v.email}"
  }
}

# --- publishing to the changes topic ----------------------------------------
#
# api       submits what a reviewer proposes.
# webhook   submits what a manufacturer republished.
# worker    publishes too, which is easy to miss: the nightly job raises expiry
#           requests onto the same topic rather than editing records directly,
#           and that is what gives an expiry the same audit trail as any other
#           change. Without it, the first record to actually expire fails 403.
# dashboard submits a person's verdict on a parked change.
resource "google_pubsub_topic_iam_member" "publishers" {
  for_each = toset(["api", "webhook", "worker", "dashboard"])
  topic    = google_pubsub_topic.changes.name
  role     = "roles/pubsub.publisher"
  member   = local.sa_member[each.value]
}

# --- BigQuery ---------------------------------------------------------------
#
# Dataset-level access is granted in bigquery.tf through the dataset's own
# access blocks, not here: `bq add-iam-policy-binding` on a dataset returns
# "This feature requires allowlisting" on an ordinary project, and the access
# list is the portable route. Running a query additionally needs jobUser at the
# project level, which is what these grant.
resource "google_project_iam_member" "job_user" {
  for_each = toset(["worker", "api", "scheduler", "dashboard"])
  project  = var.project
  role     = "roles/bigquery.jobUser"
  member   = local.sa_member[each.value]
}

# --- secrets ----------------------------------------------------------------

resource "google_secret_manager_secret_iam_member" "worker_reads_openai_key" {
  secret_id = google_secret_manager_secret.openai_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.sa_member["worker"]
}

resource "google_secret_manager_secret_iam_member" "webhook_reads_signing_secrets" {
  # Keyed off the variable, not off the secret resource. Using the resource as
  # the for_each map makes the KEYS unknown until apply, and terraform refuses
  # to plan that - it cannot tell how many instances it is about to manage.
  for_each = toset(var.webhook_sources)

  secret_id = google_secret_manager_secret.webhook[each.value].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.sa_member["webhook"]
}
