-- One row per nightly run.
--
-- The job answers two different questions, and they fail differently:
--   Did anything BREAK?  Requests that errored, messages in the dead-letter
--                        queue, reviews nobody has touched in a week.
--   Did anything DRIFT?  The pipeline still succeeds, but its behaviour has
--                        moved - more rejections than usual, confidence sagging,
--                        cost per decision climbing. Drift raises no errors,
--                        which is exactly why something has to go looking.

CREATE TABLE IF NOT EXISTS `curator.reconciliation_runs`
(
  run_id              STRING NOT NULL,
  run_at              TIMESTAMP NOT NULL,
  window_start        TIMESTAMP NOT NULL,
  window_end          TIMESTAMP NOT NULL,

  -- expiry
  epds_total          INT64,
  epds_expired        INT64,
  epds_expiring_90d   INT64,
  expiry_requests_raised INT64 OPTIONS(description="expiry changes this run submitted"),

  -- throughput in the window
  requests_total      INT64,
  requests_applied    INT64,
  requests_rejected   INT64,
  requests_pending    INT64,
  reviews_overdue     INT64 OPTIONS(description="pending more than 7 days"),

  -- what the model cost, and how it behaved
  model_calls         INT64,
  mean_confidence     FLOAT64,
  p95_latency_ms      INT64,
  total_cost_usd      NUMERIC,

  drift_flags         ARRAY<STRING> OPTIONS(description="human-readable reasons this window looks off"),
  needs_attention     BOOL NOT NULL
)
PARTITION BY DATE(run_at)
OPTIONS(description="One row per nightly reconciliation run.");
