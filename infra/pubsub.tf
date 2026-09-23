# The change requests topic, and a dead-letter topic for messages that keep failing.

resource "google_pubsub_topic" "changes" {
  name = var.topic
}

resource "google_pubsub_topic" "dlq" {
  name = "${var.topic}-dlq"
}

# Holds dead-lettered messages; a topic with no subscription discards them.
resource "google_pubsub_subscription" "dlq" {
  name                       = "${var.topic}-dlq-sub"
  topic                      = google_pubsub_topic.dlq.id
  message_retention_duration = "604800s" # 7 days
}

# Google's Pub/Sub service agent does the dead-lettering.
data "google_project" "this" {}

locals {
  pubsub_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "agent_publishes_to_dlq" {
  topic  = google_pubsub_topic.dlq.name
  role   = "roles/pubsub.publisher"
  member = local.pubsub_agent
}
