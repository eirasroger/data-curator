# One service account per service, each with only the permissions it needs.

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
# The worker publishes too: the nightly job queues expiry requests.
resource "google_pubsub_topic_iam_member" "publishers" {
  for_each = toset(["api", "webhook", "worker", "dashboard"])
  topic    = google_pubsub_topic.changes.name
  role     = "roles/pubsub.publisher"
  member   = local.sa_member[each.value]
}

# --- BigQuery ---------------------------------------------------------------
# Dataset access is in bigquery.tf. Running queries also needs jobUser.
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
  # Keyed on the variable so the keys are known at plan time.
  for_each = toset(var.webhook_sources)

  secret_id = google_secret_manager_secret.webhook[each.value].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.sa_member["webhook"]
}
