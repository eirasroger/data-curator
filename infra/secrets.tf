# Secret containers only, so values stay out of Terraform state. Add values with:
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
