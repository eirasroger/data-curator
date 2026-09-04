-- Which agents run over this data, what they do, and who answers for them.
--
-- An agent nobody owns and nobody has re-checked is a liability, not an asset.
-- last_reviewed and eval_pass_rate are the two columns that make that visible
-- instead of leaving it as something people vaguely remember.

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

  -- Scores from the last eval run. Recorded here so "is this agent still any
  -- good" is answerable from the registry rather than from someone's terminal.
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
