# Two topics, not one.
#
# epd-changes carries change requests. epd-changes-dlq is where a message lands
# after the worker has failed it several times. Without a dead-letter topic a
# message the worker can NEVER process redelivers forever: it burns a model call
# on every attempt and hides a real failure inside traffic that looks normal.

resource "google_pubsub_topic" "changes" {
  name = var.topic
}

resource "google_pubsub_topic" "dlq" {
  name = "${var.topic}-dlq"
}

# A topic with no subscription silently discards everything published to it.
# Without this the DLQ would be decorative and dead messages would vanish.
resource "google_pubsub_subscription" "dlq" {
  name                       = "${var.topic}-dlq-sub"
  topic                      = google_pubsub_topic.dlq.id
  message_retention_duration = "604800s" # 7 days
}

# Dead-lettering is performed by Google's own Pub/Sub service agent rather than
# by our code, so that agent is what needs publish rights on the DLQ.
data "google_project" "this" {}

locals {
  pubsub_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "agent_publishes_to_dlq" {
  topic  = google_pubsub_topic.dlq.name
  role   = "roles/pubsub.publisher"
  member = local.pubsub_agent
}
