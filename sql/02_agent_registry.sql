-- GTM-style agent registry: what agents exist, what they do, who owns them,
-- when a human last signed off, and how they last scored against the eval set.
--
-- The point of a registry is that an agent nobody owns and nobody has reviewed
-- is a liability, not an asset. `last_reviewed` and `eval_pass_rate` are the
-- two columns that make that visible instead of tribal knowledge.

CREATE TABLE IF NOT EXISTS `curator.agent_registry`
(
  agent_id          STRING NOT NULL,
  display_name      STRING NOT NULL,
  purpose           STRING NOT NULL OPTIONS(description="one sentence: what decision this agent makes"),
  owner             STRING NOT NULL OPTIONS(description="a person, not a team alias"),
  status            STRING NOT NULL OPTIONS(description="active | deprecated"),

  provider          STRING OPTIONS(description="llm provider adapter in use"),
  model             STRING,
  prompt_version    STRING,

  input_contract    STRING OPTIONS(description="what it consumes"),
  output_contract   STRING OPTIONS(description="what it emits"),

  eval_pass_rate    FLOAT64 OPTIONS(description="accuracy on the labelled eval set at eval_run_at"),
  eval_size         INT64,
  eval_run_at       TIMESTAMP,

  last_reviewed     DATE OPTIONS(description="last time a human confirmed this agent still does what it claims"),
  created_at        TIMESTAMP NOT NULL
)
OPTIONS(description="Registry of LLM agents running in this pipeline.");


-- Agents that are stale: nobody has reviewed them in 90 days, or they have
-- never been evaluated at all.
CREATE OR REPLACE VIEW `curator.agent_registry_stale` AS
SELECT
  agent_id,
  display_name,
  owner,
  last_reviewed,
  DATE_DIFF(CURRENT_DATE(), last_reviewed, DAY) AS days_since_review,
  eval_pass_rate,
  CASE
    WHEN eval_run_at IS NULL THEN 'never evaluated'
    WHEN last_reviewed IS NULL THEN 'never reviewed'
    WHEN DATE_DIFF(CURRENT_DATE(), last_reviewed, DAY) > 90 THEN 'review overdue'
  END AS reason
FROM `curator.agent_registry`
WHERE status = 'active'
  AND (
    eval_run_at IS NULL
    OR last_reviewed IS NULL
    OR DATE_DIFF(CURRENT_DATE(), last_reviewed, DAY) > 90
  );
