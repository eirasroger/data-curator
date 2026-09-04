-- Nightly reconciliation, run by Cloud Scheduler.
--
-- Two different questions, deliberately separated:
--   1. Did anything FAIL?  (status != 'ok', or landed in the dead-letter queue)
--   2. Did anything DRIFT? (the agent still succeeds, but its behaviour has
--      moved - confidence sagging, one class suddenly dominating, cost per
--      event climbing). Drift is the failure mode that does not raise errors,
--      which is exactly why a scheduled job has to go looking for it.

CREATE TABLE IF NOT EXISTS `curator.reconciliation_runs`
(
  run_id              STRING NOT NULL,
  run_at              TIMESTAMP NOT NULL,
  window_start        TIMESTAMP NOT NULL,
  window_end          TIMESTAMP NOT NULL,
  events_total        INT64,
  events_ok           INT64,
  events_failed       INT64,
  events_low_conf     INT64,
  mean_confidence     FLOAT64,
  p95_latency_ms      INT64,
  total_cost_usd      NUMERIC,
  drift_flags         ARRAY<STRING> OPTIONS(description="human-readable reasons this window looks off"),
  needs_attention     BOOL NOT NULL
)
PARTITION BY DATE(run_at)
OPTIONS(description="One row per nightly reconciliation run.");
