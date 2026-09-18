-- DuckDB port of sql/*.sql, for running without Google Cloud.
--
-- Differences from the BigQuery DDL, all of them forced:
--   PARTITION BY / CLUSTER BY are BigQuery storage directives and have no
--     DuckDB equivalent. They affect cost and speed there, nothing here.
--   OPTIONS(description=...) is BigQuery syntax. The column comments live in
--     sql/*.sql, which stays the reference.
--   INT64/FLOAT64/BOOL become BIGINT/DOUBLE/BOOLEAN.
--   ARRAY<STRING> becomes VARCHAR[].
--   NUMERIC becomes DECIMAL(18, 9).
--
-- Append-only is enforced by the code here, not by the platform. BigQuery gets
-- it for free from the streaming buffer; nothing stops an UPDATE in DuckDB, so
-- the only protection is that no method issues one.

CREATE TABLE IF NOT EXISTS epd_records
(
  product_id        BIGINT NOT NULL,
  version           BIGINT NOT NULL,
  valid_from        TIMESTAMPTZ NOT NULL,
  change_request_id VARCHAR,
  epd_code          VARCHAR,
  prod_name         VARCHAR,
  prod_man          VARCHAR,
  prod_site         VARCHAR,
  expiry_date       DATE,
  status            VARCHAR NOT NULL,
  reference_unit    VARCHAR,
  density           DOUBLE,
  thickness         DOUBLE,
  lifespan          DOUBLE,
  gwp_total         DOUBLE,
  gwp_fossil        DOUBLE,
  gwp_luluc         DOUBLE,
  gwp_bio           DOUBLE,
  record            JSON NOT NULL
);

CREATE OR REPLACE VIEW epd_current AS
SELECT * FROM epd_records
QUALIFY ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY version DESC) = 1;

CREATE OR REPLACE VIEW epd_expiry_status AS
SELECT
  product_id,
  epd_code,
  prod_name,
  prod_man,
  expiry_date,
  date_diff('day', CURRENT_DATE, expiry_date) AS days_until_expiry,
  CASE
    WHEN expiry_date IS NULL                                  THEN 'no expiry date'
    WHEN expiry_date < CURRENT_DATE                           THEN 'expired'
    WHEN date_diff('day', CURRENT_DATE, expiry_date) <= 90    THEN 'expiring within 90 days'
    WHEN date_diff('day', CURRENT_DATE, expiry_date) <= 365   THEN 'expiring within a year'
    ELSE 'valid'
  END AS expiry_state
FROM epd_current
WHERE status != 'superseded';

CREATE TABLE IF NOT EXISTS change_requests
(
  request_id    VARCHAR NOT NULL,
  product_id    BIGINT  NOT NULL,
  kind          VARCHAR NOT NULL,
  source        VARCHAR NOT NULL,
  submitted_by  VARCHAR NOT NULL,
  submitted_at  TIMESTAMPTZ NOT NULL,
  field_path    VARCHAR,
  new_value     JSON,
  replacement   JSON,
  reason        VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS change_events
(
  event_id      VARCHAR NOT NULL,
  request_id    VARCHAR NOT NULL,
  product_id    BIGINT  NOT NULL,
  occurred_at   TIMESTAMPTZ NOT NULL,
  event_type    VARCHAR NOT NULL,
  actor         VARCHAR NOT NULL,
  action        VARCHAR NOT NULL,
  reason        VARCHAR NOT NULL,
  blocking_issues VARCHAR[],
  old_value     JSON,
  triage        VARCHAR,
  confidence    DOUBLE,
  rationale     VARCHAR,
  model         VARCHAR,
  prompt_tokens BIGINT,
  completion_tokens BIGINT,
  cost_usd      DECIMAL(18, 9),
  latency_ms    BIGINT
);

CREATE OR REPLACE VIEW change_status AS
SELECT
  r.request_id, r.product_id, r.kind, r.source, r.submitted_by, r.submitted_at,
  r.field_path, r.new_value, r.reason AS submitted_reason,
  e.action, e.reason AS decision_reason, e.event_type, e.actor,
  e.occurred_at AS decided_at, e.triage, e.confidence, e.rationale,
  e.model, e.cost_usd, e.latency_ms
FROM change_requests r
LEFT JOIN (
  SELECT * FROM change_events
  QUALIFY ROW_NUMBER() OVER (PARTITION BY request_id ORDER BY occurred_at DESC) = 1
) e USING (request_id);

CREATE OR REPLACE VIEW review_queue AS
SELECT
  request_id, product_id, kind, field_path, new_value, submitted_by,
  submitted_reason, triage, confidence, rationale, decision_reason,
  submitted_at,
  date_diff('hour', submitted_at, CURRENT_TIMESTAMP) AS hours_waiting
FROM change_status
WHERE action = 'pending_review'
ORDER BY submitted_at;

CREATE TABLE IF NOT EXISTS agent_registry
(
  agent_id        VARCHAR NOT NULL,
  display_name    VARCHAR NOT NULL,
  purpose         VARCHAR NOT NULL,
  owner           VARCHAR NOT NULL,
  status          VARCHAR NOT NULL,
  provider        VARCHAR,
  model           VARCHAR,
  prompt_version  VARCHAR,
  input_contract  VARCHAR,
  output_contract VARCHAR,
  eval_pass_rate  DOUBLE,
  eval_unsafe     BIGINT,
  eval_size       BIGINT,
  eval_run_at     TIMESTAMPTZ,
  last_reviewed   DATE,
  created_at      TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE VIEW agent_registry_attention AS
SELECT
  agent_id, display_name, owner, last_reviewed, eval_pass_rate, eval_unsafe,
  date_diff('day', last_reviewed, CURRENT_DATE) AS days_since_review,
  CASE
    WHEN eval_unsafe > 0                                        THEN 'unsafe auto-applies in last eval'
    WHEN eval_run_at IS NULL                                    THEN 'never evaluated'
    WHEN last_reviewed IS NULL                                  THEN 'never reviewed'
    WHEN date_diff('day', last_reviewed, CURRENT_DATE) > 90     THEN 'review overdue'
  END AS reason
FROM agent_registry
WHERE status = 'active'
  AND (eval_unsafe > 0 OR eval_run_at IS NULL OR last_reviewed IS NULL
       OR date_diff('day', last_reviewed, CURRENT_DATE) > 90);

CREATE TABLE IF NOT EXISTS reconciliation_runs
(
  run_id              VARCHAR NOT NULL,
  run_at              TIMESTAMPTZ NOT NULL,
  window_start        TIMESTAMPTZ NOT NULL,
  window_end          TIMESTAMPTZ NOT NULL,
  epds_total          BIGINT,
  epds_expired        BIGINT,
  epds_expiring_90d   BIGINT,
  expiry_requests_raised BIGINT,
  requests_total      BIGINT,
  requests_applied    BIGINT,
  requests_rejected   BIGINT,
  requests_pending    BIGINT,
  reviews_overdue     BIGINT,
  model_calls         BIGINT,
  mean_confidence     DOUBLE,
  p95_latency_ms      BIGINT,
  total_cost_usd      DECIMAL(18, 9),
  drift_flags         VARCHAR[],
  needs_attention     BOOLEAN NOT NULL
);
