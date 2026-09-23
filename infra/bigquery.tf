# The dataset, tables and views. Schemas are in schemas/, view SQL in views/.

resource "google_bigquery_dataset" "curator" {
  dataset_id  = var.dataset
  location    = var.bq_location
  description = "EPD golden record, change log and agent registry"

  # Access is set here, since dataset IAM bindings need allowlisting.
  # The worker is the only writer.
  access {
    role          = "WRITER"
    user_by_email = google_service_account.sa["worker"].email
  }

  access {
    role          = "READER"
    user_by_email = google_service_account.sa["api"].email
  }

  access {
    role          = "READER"
    user_by_email = google_service_account.sa["dashboard"].email
  }

  # BigQuery's defaults, restated because this list replaces all access on apply.
  access {
    role          = "OWNER"
    special_group = "projectOwners"
  }

  access {
    role          = "WRITER"
    special_group = "projectWriters"
  }

  access {
    role          = "READER"
    special_group = "projectReaders"
  }

  # Extra named owners, usually none.
  dynamic "access" {
    for_each = toset(var.dataset_owners)
    content {
      role          = "OWNER"
      user_by_email = access.value
    }
  }
}

locals {
  # Dataset-qualified names match what BigQuery stores, so applies stay clean.
  view_vars = {
    dataset = var.dataset
  }

  tables = {
    epd_records = {
      description  = "Versioned EPD records. Query epd_current for the latest of each."
      clustering   = ["product_id"]
      partition_on = null
    }
    change_requests = {
      description  = "Immutable record of every proposed change."
      clustering   = ["product_id"]
      partition_on = "submitted_at"
    }
    change_events = {
      description  = "Append-only log of decisions and reviews."
      clustering   = ["request_id", "action"]
      partition_on = "occurred_at"
    }
    agent_registry = {
      description  = "Registry of the agents in this pipeline."
      clustering   = null
      partition_on = null
    }
    reconciliation_runs = {
      description  = "One row per nightly reconciliation run."
      clustering   = null
      partition_on = "run_at"
    }
  }
}

resource "google_bigquery_table" "tables" {
  for_each = local.tables

  dataset_id  = google_bigquery_dataset.curator.dataset_id
  table_id    = each.key
  description = each.value.description
  schema      = file("${path.module}/schemas/${each.key}.json")
  clustering  = each.value.clustering

  dynamic "time_partitioning" {
    for_each = each.value.partition_on == null ? [] : [each.value.partition_on]
    content {
      type  = "DAY"
      field = time_partitioning.value
    }
  }

  # Protects the audit trail from `terraform destroy`.
  deletion_protection = true
}

# Two groups, because epd_expiry_status and review_queue read other views,
# which must exist first.

locals {
  base_views      = ["epd_current", "change_status", "agent_registry_attention"]
  dependent_views = ["epd_expiry_status", "review_queue"]
}

resource "google_bigquery_table" "views" {
  for_each = toset(local.base_views)

  dataset_id = google_bigquery_dataset.curator.dataset_id
  table_id   = each.value

  view {
    query          = trimspace(templatefile("${path.module}/views/${each.value}.sql", local.view_vars))
    use_legacy_sql = false
  }

  deletion_protection = false

  depends_on = [google_bigquery_table.tables]
}

resource "google_bigquery_table" "dependent_views" {
  for_each = toset(local.dependent_views)

  dataset_id = google_bigquery_dataset.curator.dataset_id
  table_id   = each.value

  view {
    query          = trimspace(templatefile("${path.module}/views/${each.value}.sql", local.view_vars))
    use_legacy_sql = false
  }

  deletion_protection = false

  depends_on = [google_bigquery_table.views]
}
