variable "project" {
  description = "GCP project id. Same default as scripts/gcp_env.sh."
  type        = string
  default     = "data-curator-507614"
}

variable "region" {
  description = "Cloud Run, Scheduler and Artifact Registry region."
  type        = string
  default     = "europe-west1"
}

variable "bq_location" {
  description = "BigQuery dataset location. The data stays in the EU."
  type        = string
  default     = "EU"
}

variable "dataset" {
  description = "BigQuery dataset name."
  type        = string
  default     = "curator"
}

variable "topic" {
  description = "Pub/Sub topic carrying change requests."
  type        = string
  default     = "epd-changes"
}

variable "webhook_sources" {
  description = "Manufacturer feeds allowed to post to the webhook. One signing secret each."
  type        = list(string)
  default     = ["manufacturer"]
}

variable "reconcile_schedule" {
  description = "Cron for the nightly job. Created paused; see scheduler.tf."
  type        = string
  default     = "0 2 * * *"
}

variable "reconcile_timezone" {
  type    = string
  default = "Europe/Madrid"
}

variable "worker_url" {
  description = "The deployed worker's URL. The scheduler job is created once this is set."
  type        = string
  default     = ""
}

variable "dataset_owners" {
  description = "Extra dataset owners by email. Project owners already have access."
  type        = list(string)
  default     = []
}
