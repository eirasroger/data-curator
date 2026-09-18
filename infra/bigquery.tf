# The dataset, its tables and its views.
#
# Schemas live in schemas/*.json and view bodies in views/*.sql, both extracted
# from the running dataset rather than retyped, so what terraform creates is
# what the bash scripts built. sql/*.sql stays as the readable reference with
# its rationale comments; these are the machine's copy.

resource "google_bigquery_dataset" "curator" {
  dataset_id  = var.dataset
  location    = var.bq_location
  description = "EPD golden record, change log and agent registry"

  # Dataset-level access goes here rather than through IAM bindings.
  # `bq add-iam-policy-binding` on a dataset returns "This feature requires
  # allowlisting" on an ordinary project, and because the old setup script
  # tolerated that failure the grants silently did not happen - discovered from
  # a 403 in the logs much later. The access list is the portable route, and
  # expressing it here means it cannot be skipped.

  # The worker is the ONLY writer. One writer means one place where the
  # append-only rule can be broken, and one place to look when it is.
  access {
    role          = "WRITER"
    user_by_email = google_service_account.sa["worker"].email
  }

  # Everything else reads. The API serves records and the review queue; the
  # dashboard shows them. Neither can write.
  access {
    role          = "READER"
    user_by_email = google_service_account.sa["api"].email
  }

  access {
    role          = "READER"
    user_by_email = google_service_account.sa["dashboard"].email
  }

  # BigQuery's own defaults, restated because this list is AUTHORITATIVE:
  # anything not declared here is removed on apply. The first plan after
  # importing the live dataset wanted to delete all three of these, which would
  # have quietly changed who can read and write the data.
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

  # Named people, for the same reason. A project owner already has OWNER
  # through projectOwners above, so this is usually empty.
  dynamic "access" {
    for_each = toset(var.dataset_owners)
    content {
      role          = "OWNER"
      user_by_email = access.value
    }
  }
}

locals {
  # Dataset-qualified, which is the form BigQuery stores for a view reading a
  # table in its own dataset. Project-qualifying works too but differs from
  # every deployed view, so every apply would rewrite all five for nothing.
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

  # These hold the audit trail. Refusing to destroy them means a stray
  # `terraform destroy` cannot take the history with it; removing data is a
  # deliberate act, not a side effect of tearing down compute.
  deletion_protection = true
}

# Views come in two waves because two of them read other views:
# epd_expiry_status reads epd_current, review_queue reads change_status.
# BigQuery rejects a view whose source does not exist yet, and terraform cannot
# see that dependency inside a SQL string - with everything in one for_each it
# creates all five in parallel and two fail on a cold apply.

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

  # A view holds no data, so dropping one costs nothing but a re-apply.
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
