SELECT
  r.request_id, r.product_id, r.kind, r.source, r.submitted_by, r.submitted_at,
  r.field_path, r.new_value, r.reason AS submitted_reason,
  e.action, e.reason AS decision_reason, e.event_type, e.actor,
  e.occurred_at AS decided_at, e.triage, e.confidence, e.rationale,
  e.model, e.cost_usd, e.latency_ms
FROM `${dataset}.change_requests` r
LEFT JOIN (
  SELECT * EXCEPT(_rn) FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY request_id ORDER BY occurred_at DESC) AS _rn
    FROM `${dataset}.change_events`
  ) WHERE _rn = 1
) e USING (request_id)
