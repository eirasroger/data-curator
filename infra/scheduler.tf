# The nightly reconciliation job.
#
# Skipped while worker_url is empty. Cloud Run revisions are build artifacts
# and belong to deploy.sh rather than to state, so terraform cannot know the
# worker's address until after a deploy. Pass it on the second apply:
#
#   terraform apply -var worker_url="$(gcloud run services describe \
#     curator-worker --region=europe-west1 --format='value(status.url)')"

locals {
  scheduler_enabled = var.worker_url != ""
}

resource "google_cloud_scheduler_job" "reconcile" {
  count = local.scheduler_enabled ? 1 : 0

  name      = "curator-reconcile"
  region    = var.region
  schedule  = var.reconcile_schedule
  time_zone = var.reconcile_timezone

  # 10 minutes. The job walks every record and can raise change requests; the
  # 3-minute default cuts it off mid-run on a slow night.
  attempt_deadline = "600s"

  retry_config {
    retry_count = 3
  }

  http_target {
    uri         = "${var.worker_url}/jobs/reconcile"
    http_method = "POST"

    # A POST with no body is answered 411 Length Required by Google's front end
    # before it ever reaches Cloud Run. An empty JSON object is enough to set
    # Content-Length; the endpoint ignores what is in it.
    body = base64encode("{}")

    oidc_token {
      service_account_email = google_service_account.sa["scheduler"].email
      # Must be the service's base URL. Cloud Run checks the token's audience
      # against itself, and a token minted for the wrong audience is rejected
      # with a 401 that looks exactly like a permissions problem.
      audience = var.worker_url
    }
  }

  # Created paused, and left that way. An unattended job firing every night is a
  # standing liability on a project nobody is watching, and every check it
  # performs can be run on demand:
  #
  #   gcloud scheduler jobs run curator-reconcile --location=europe-west1
  #
  # terraform has no "paused" attribute, so the pause is applied out of band and
  # the resulting state ignored here - otherwise every plan would show a diff
  # trying to resume it.
  paused = true

  lifecycle {
    ignore_changes = [paused]
  }
}

# The scheduler calls the worker with its own identity. The endpoint is not
# protected by a shared secret or an API key - Cloud Run IAM decides who may
# call it - so there is nothing to leak and nothing to rotate.
#
# Granted only once the service exists, for the same reason as the job itself.
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
