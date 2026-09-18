SELECT
  request_id, product_id, kind, field_path, new_value, submitted_by,
  submitted_reason, triage, confidence, rationale, decision_reason,
  submitted_at,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), submitted_at, HOUR) AS hours_waiting
FROM `${dataset}.change_status`
WHERE action = 'pending_review'
ORDER BY submitted_at
