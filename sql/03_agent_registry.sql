-- The agents that act on this data, with owner, eval scores and last review date.

CREATE TABLE IF NOT EXISTS `curator.agent_registry`
(
  agent_id        STRING NOT NULL,
  display_name    STRING NOT NULL,
  purpose         STRING NOT NULL OPTIONS(description="one sentence: what decision this agent makes"),
  owner           STRING NOT NULL OPTIONS(description="a person, not a team alias"),
  status          STRING NOT NULL OPTIONS(description="active | deprecated"),

  provider        STRING,
  model           STRING,
  prompt_version  STRING,
  input_contract  STRING,
  output_contract STRING,

  -- Scores from the latest eval run.
  eval_pass_rate  FLOAT64,
  eval_unsafe     INT64  OPTIONS(description="unsafe auto-applies in the last run; must be 0"),
  eval_size       INT64,
  eval_run_at     TIMESTAMP,

  last_reviewed   DATE,
  created_at      TIMESTAMP NOT NULL
)
OPTIONS(description="Registry of the agents in this pipeline.");


CREATE OR REPLACE VIEW `curator.agent_registry_attention` AS
SELECT
  agent_id, display_name, owner, last_reviewed, eval_pass_rate, eval_unsafe,
  DATE_DIFF(CURRENT_DATE(), last_reviewed, DAY) AS days_since_review,
  CASE
    WHEN eval_unsafe > 0                                          THEN 'unsafe auto-applies in last eval'
    WHEN eval_run_at IS NULL                                      THEN 'never evaluated'
    WHEN last_reviewed IS NULL                                    THEN 'never reviewed'
    WHEN DATE_DIFF(CURRENT_DATE(), last_reviewed, DAY) > 90       THEN 'review overdue'
  END AS reason
FROM `curator.agent_registry`
WHERE status = 'active'
  AND (eval_unsafe > 0 OR eval_run_at IS NULL OR last_reviewed IS NULL
       OR DATE_DIFF(CURRENT_DATE(), last_reviewed, DAY) > 90);
