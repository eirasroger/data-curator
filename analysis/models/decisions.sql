-- One row per decision, with the things the analysis groups by.
--
-- Written against the same tables as sql/local/schema.sql, so this ports to
-- BigQuery by re-quoting the table names. Kept as SQL rather than pandas on
-- purpose: the aggregation belongs where the data is.

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
  -- The shape that produced this proposal is encoded in the request id the
  -- simulator assigns: sim-<tag>-<n>. It is the only way to recover intent
  -- from the stored row, and the analysis needs it to say WHICH proposals the
  -- rules absorb.
  split_part(r.request_id, '-', 2)    AS shape_tag,
  -- Did the model get consulted at all? A null confidence means the rules
  -- settled it before any model was asked.
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


-- Which validation checks actually fire, and how often. blocking_issues is an
-- array of "field: detail" strings, so unnest first and keep the field.
CREATE OR REPLACE VIEW rejection_reasons AS
SELECT
  CASE
    -- A malformed replacement carries a pydantic message, not a "field: detail"
    -- pair, so splitting on the colon would put its prose in the field column.
    WHEN issue LIKE '%validation error%' THEN '(replacement fails schema)'
    ELSE split_part(issue, ':', 1)
  END      AS failing_field,
  count(*) AS times
FROM (SELECT unnest(blocking_issues) AS issue FROM decisions)
GROUP BY 1
ORDER BY times DESC;


-- Throughput per day, for the drift charts. Rates, not counts, so a quiet day
-- and a busy day are comparable.
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
