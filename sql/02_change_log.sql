-- The audit trail: every proposal ever made, and everything that happened to it.
--
-- Two tables, both append-only, and the split is deliberate.
--
--   change_requests  what somebody asked for. Immutable - a request never
--                    changes after it is submitted.
--   change_events    what the system and its reviewers DID about it. A request
--                    can collect several: decided by the pipeline, then
--                    approved or rejected by a person days later.
--
-- Keeping them apart means a review is a new fact appended to history, not an
-- edit that overwrites what the system originally concluded. On compliance data
-- you need both answers: what the machine decided, and what the human decided
-- afterwards.

CREATE TABLE IF NOT EXISTS `curator.change_requests`
(
  request_id    STRING NOT NULL,
  product_id    INT64  NOT NULL,
  kind          STRING NOT NULL OPTIONS(description="field_update | record_replacement | expiry"),
  source        STRING NOT NULL OPTIONS(description="human | manufacturer_feed | scheduler"),
  submitted_by  STRING NOT NULL,
  submitted_at  TIMESTAMP NOT NULL,

  field_path    STRING OPTIONS(description="e.g. 'density', 'impacts.gwp_total'"),
  new_value     JSON   OPTIONS(description="JSON so a number, a string or null are all the same column"),
  replacement   JSON   OPTIONS(description="the whole new record, for record_replacement"),
  reason        STRING NOT NULL OPTIONS(description="why the submitter thinks this is right")
)
PARTITION BY DATE(submitted_at)
CLUSTER BY product_id
OPTIONS(description="Immutable record of every proposed change.");


CREATE TABLE IF NOT EXISTS `curator.change_events`
(
  event_id      STRING NOT NULL,
  request_id    STRING NOT NULL,
  product_id    INT64  NOT NULL,
  occurred_at   TIMESTAMP NOT NULL,
  event_type    STRING NOT NULL OPTIONS(description="decided (by the pipeline) | reviewed (by a person)"),
  actor         STRING NOT NULL OPTIONS(description="'pipeline', or who reviewed it"),

  action        STRING NOT NULL OPTIONS(description="applied | rejected | pending_review"),
  reason        STRING NOT NULL OPTIONS(description="which rule produced this, in words"),
  blocking_issues ARRAY<STRING> OPTIONS(description="validation errors the change would introduce"),
  old_value     JSON,

  -- Present only when the model was actually consulted. NULL here is meaningful:
  -- it says the rules settled the request without spending anything.
  triage        STRING,
  confidence    FLOAT64,
  rationale     STRING,
  model         STRING,
  prompt_tokens INT64,
  completion_tokens INT64,
  cost_usd      NUMERIC,
  latency_ms    INT64
)
PARTITION BY DATE(occurred_at)
CLUSTER BY request_id, action
OPTIONS(description="Append-only log of decisions and reviews.");


-- Where a request stands right now: its most recent event.
CREATE OR REPLACE VIEW `curator.change_status` AS
SELECT
  r.request_id, r.product_id, r.kind, r.source, r.submitted_by, r.submitted_at,
  r.field_path, r.new_value, r.reason AS submitted_reason,
  e.action, e.reason AS decision_reason, e.event_type, e.actor,
  e.occurred_at AS decided_at, e.triage, e.confidence, e.rationale,
  e.model, e.cost_usd, e.latency_ms
FROM `curator.change_requests` r
LEFT JOIN (
  SELECT * EXCEPT(_rn) FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY request_id ORDER BY occurred_at DESC) AS _rn
    FROM `curator.change_events`
  ) WHERE _rn = 1
) e USING (request_id);


-- The human work queue: what is waiting for a person, oldest first.
CREATE OR REPLACE VIEW `curator.review_queue` AS
SELECT
  request_id, product_id, kind, field_path, new_value, submitted_by,
  submitted_reason, triage, confidence, rationale, decision_reason,
  submitted_at,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), submitted_at, HOUR) AS hours_waiting
FROM `curator.change_status`
WHERE action = 'pending_review'
ORDER BY submitted_at;
