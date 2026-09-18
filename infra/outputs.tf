output "service_accounts" {
  description = "Each service's identity, for deploy.sh to attach."
  value       = { for k, v in google_service_account.sa : k => v.email }
}

output "topic" {
  value = google_pubsub_topic.changes.name
}

output "dlq_topic" {
  value = google_pubsub_topic.dlq.name
}

output "dataset" {
  value = google_bigquery_dataset.curator.dataset_id
}

output "next_steps" {
  description = "What terraform deliberately does not do."
  value = local.scheduler_enabled ? "Infrastructure complete." : join("\n", [
    "1. gcloud secrets versions add openai-api-key --data-file=-",
    "2. python scripts/seed_bigquery.py",
    "3. bash scripts/deploy.sh",
    "4. terraform apply -var worker_url=<the worker's URL>   # adds the scheduler",
  ])
}
