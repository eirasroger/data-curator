-- Views for analysis/report.py, one row per decision. Uses the tables in
-- sql/local/schema.sql.

CREATE OR REPLACE VIEW decisions AS
SELECT
  e.event_id,
  e.request_id,
  e.product_id,
  e.occurred_at,
  e.action,
  e.reason,
  e.triage,
  e.confidence,
  e.model,
  e.cost_usd,
  e.latency_ms,
  len(e.blocking_issues)              AS blocking_issue_count,
  e.blocking_issues,
  r.kind,
  r.source,
  r.field_path,
  r.submitted_at,
  -- Proposal shape, from the simulator's request id: sim-<tag>-<n>.
  split_part(r.request_id, '-', 2)    AS shape_tag,
  -- A null confidence means the rules decided alone.
  e.confidence IS NOT NULL            AS reached_model,
  CASE
    WHEN e.confidence IS NOT NULL THEN 'model'
    ELSE 'rules'
  END                                  AS settled_by,
  date_trunc('day', e.occurred_at)     AS day
FROM change_events e
JOIN change_requests r USING (request_id)
WHERE e.event_type = 'decided';


-- Where the work actually gets done, by the field being changed.
CREATE OR REPLACE VIEW work_by_field AS
SELECT
  coalesce(field_path, '(whole record)') AS field,
  count(*)                               AS decisions,
  count_if(settled_by = 'rules')         AS by_rules,
  count_if(settled_by = 'model')          AS by_model,
  count_if(action = 'applied')            AS applied,
  count_if(action = 'rejected')           AS rejected,
  count_if(action = 'pending_review')     AS pending
FROM decisions
GROUP BY 1
ORDER BY decisions DESC;


-- How often each validation check fires. Each blocking issue is "field: detail".
CREATE OR REPLACE VIEW rejection_reasons AS
SELECT
  CASE
    -- Schema failures on replacements carry free text, so label them separately.
    WHEN issue LIKE '%validation error%' THEN '(replacement fails schema)'
    ELSE split_part(issue, ':', 1)
  END      AS failing_field,
  count(*) AS times
FROM (SELECT unnest(blocking_issues) AS issue FROM decisions)
GROUP BY 1
ORDER BY times DESC;


-- Daily rates for the drift chart.
CREATE OR REPLACE VIEW daily AS
SELECT
  day,
  count(*)                                        AS decisions,
  count_if(action = 'rejected')                   AS rejected,
  count_if(action = 'rejected') * 1.0 / count(*)  AS rejection_rate,
  count_if(action = 'applied')  * 1.0 / count(*)  AS apply_rate,
  count_if(settled_by = 'model') * 1.0 / count(*) AS model_rate,
  avg(confidence)                                 AS mean_confidence
FROM decisions
GROUP BY 1
ORDER BY day;
