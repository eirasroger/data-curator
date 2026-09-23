# The nightly job. Created only once worker_url is set, after deploy.sh has run.

locals {
  scheduler_enabled = var.worker_url != ""
}

resource "google_cloud_scheduler_job" "reconcile" {
  count = local.scheduler_enabled ? 1 : 0

  name      = "curator-reconcile"
  region    = var.region
  schedule  = var.reconcile_schedule
  time_zone = var.reconcile_timezone

  # The 3-minute default can cut the job short.
  attempt_deadline = "600s"

  retry_config {
    retry_count = 3
  }

  http_target {
    uri         = "${var.worker_url}/jobs/reconcile"
    http_method = "POST"

    # A body is required; an empty POST gets 411 from Google's front end.
    body = base64encode("{}")

    oidc_token {
      service_account_email = google_service_account.sa["scheduler"].email
      # Must be the service's base URL, or Cloud Run returns 401.
      audience = var.worker_url
    }
  }

  # Created paused. Run on demand:
  #   gcloud scheduler jobs run curator-reconcile --location=europe-west1
  paused = true

  lifecycle {
    ignore_changes = [paused]
  }
}

# Invoker grants, created once the worker exists.
resource "google_cloud_run_service_iam_member" "scheduler_invokes_worker" {
  count = local.scheduler_enabled ? 1 : 0

  location = var.region
  service  = "curator-worker"
  role     = "roles/run.invoker"
  member   = local.sa_member["scheduler"]
}

resource "google_cloud_run_service_iam_member" "pubsub_invokes_worker" {
  count = local.scheduler_enabled ? 1 : 0

  location = var.region
  service  = "curator-worker"
  role     = "roles/run.invoker"
  member   = local.sa_member["pubsub"]
}
