# Secret containers only, never versions.
#
# The value of a secret is not infrastructure and does not belong in state - a
# terraform state file holds resource attributes in the clear, so a managed
# secret version would put the API key in a file on disk. The container is
# created here; the value is added out of band:
#
#   gcloud secrets versions add openai-api-key --data-file=-

resource "google_secret_manager_secret" "openai_api_key" {
  secret_id = "openai-api-key"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "webhook" {
  for_each  = toset(var.webhook_sources)
  secret_id = "webhook-secret-${each.value}"

  replication {
    auto {}
  }
}
