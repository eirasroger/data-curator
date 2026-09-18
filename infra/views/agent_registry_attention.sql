SELECT
  agent_id, display_name, owner, last_reviewed, eval_pass_rate, eval_unsafe,
  DATE_DIFF(CURRENT_DATE(), last_reviewed, DAY) AS days_since_review,
  CASE
    WHEN eval_unsafe > 0                                          THEN 'unsafe auto-applies in last eval'
    WHEN eval_run_at IS NULL                                      THEN 'never evaluated'
    WHEN last_reviewed IS NULL                                    THEN 'never reviewed'
    WHEN DATE_DIFF(CURRENT_DATE(), last_reviewed, DAY) > 90       THEN 'review overdue'
  END AS reason
FROM `${dataset}.agent_registry`
WHERE status = 'active'
  AND (eval_unsafe > 0 OR eval_run_at IS NULL OR last_reviewed IS NULL
       OR DATE_DIFF(CURRENT_DATE(), last_reviewed, DAY) > 90)
