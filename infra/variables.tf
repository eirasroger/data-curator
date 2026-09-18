variable "project" {
  description = "GCP project id. Same default as scripts/gcp_env.sh."
  type        = string
  default     = "data-curator-507614"
}

variable "region" {
  description = "Cloud Run, Scheduler and Artifact Registry region."
  type        = string
  default     = "europe-west1" # Belgium: nearest EU region with everything needed
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
  description = <<-EOT
    Manufacturer feeds that may post signed republications. One signing secret
    per source, so a leaked key compromises one sender and revoking a sender is
    one secret version.
  EOT
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
  description = <<-EOT
    The deployed worker's base URL, which the scheduler posts to. Empty until
    the service exists, and the scheduler job is skipped while it is empty:
    Cloud Run revisions are build artifacts and belong to deploy.sh, not to
    state, so terraform cannot know this until after a deploy.
  EOT
  type        = string
  default     = ""
}

variable "dataset_owners" {
  description = <<-EOT
    Extra people granted OWNER on the dataset by email. Project owners already
    hold it through the projectOwners group, so this is normally empty; set it
    only to keep an explicit grant that predates terraform.
  EOT
  type        = list(string)
  default     = []
}
