-- Periodic governance survey + improvement loop.
-- <DS_FEEDBACK> is substituted from the DS_FEEDBACK env var by scripts/init_bigquery.py.

-- Survey responses — summary only (no verbatim prompt; mirrors the ledger T6 invariant).
-- Pseudonymous by default: respondent_token = hash(sub); respondent_id only in identified mode.
CREATE TABLE IF NOT EXISTS `<DS_FEEDBACK>.survey_responses` (
  response_id       STRING NOT NULL,        -- UUID
  occurred_at       TIMESTAMP NOT NULL,
  campaign_id       STRING NOT NULL,        -- survey period, e.g. 2026-06 (monthly)
  respondent_token  STRING NOT NULL,        -- one-way hash of OAuth sub (pseudonymous)
  respondent_id     STRING,                 -- persons.person_id (identified mode only)
  nps_score         INT64 NOT NULL,         -- 0..10
  nps_reason        STRING,
  useful_summary    STRING,                 -- useful AI answer (summary)
  harmful_summary   STRING,                 -- harmful/incorrect AI answer (summary)
  incident_flag     BOOL NOT NULL,
  incident_summary  STRING,                 -- potential-incident content (sensitive, summary)
  contact_opt_in    BOOL NOT NULL,          -- consent to follow-up on an incident
  ref_log_id        STRING,                 -- self-reported usage_log link (aids 特定)
  ref_request_id    STRING,
  scope_sensitivity STRING NOT NULL,        -- normal|sensitive (incident -> sensitive, §3.3)
  owner_unit        STRING,
  -- §3.3: 申告者が自部門を迂回して送ったか。スチュワードは自部門へ差し戻してはならない。
  -- NULL は「意図が記録されていない」行であって false ではない。
  -- 読み手は `NOT escalated` ではなく `escalated IS NOT TRUE` を使うこと。
  escalated         BOOL,
  created_at        TIMESTAMP NOT NULL
)
PARTITION BY DATE(occurred_at);

-- Improvement tasks — derived from 特定 (identify) of survey feedback.
CREATE TABLE IF NOT EXISTS `<DS_FEEDBACK>.improvement_tasks` (
  task_id            STRING NOT NULL,       -- UUID
  created_at         TIMESTAMP NOT NULL,
  updated_at         TIMESTAMP NOT NULL,
  source_response_id STRING NOT NULL,
  kind               STRING NOT NULL,       -- harmful|incident|nps_detractor|term_gap|sfaq_gap
  evidence           STRING,
  target_kind        STRING NOT NULL,       -- term|sfaq|usage_log|none
  target_id          STRING,
  status             STRING NOT NULL,       -- open|triaged|in_progress|resolved|wontfix
  priority           INT64 NOT NULL,        -- higher = more urgent
  owner_unit         STRING
)
PARTITION BY DATE(created_at);

-- Extraction candidates — term/SFAQ/ERN material proposed by the AI, awaiting user approval.
CREATE TABLE IF NOT EXISTS `<DS_FEEDBACK>.extraction_candidates` (
  candidate_id      STRING NOT NULL,        -- UUID
  created_at        TIMESTAMP NOT NULL,
  updated_at        TIMESTAMP NOT NULL,
  kind              STRING NOT NULL,        -- term|sfaq|ern
  source            STRING NOT NULL,        -- survey|prompt|summary
  source_ref        STRING,
  payload           JSON,                   -- term/definition | question/decision/reason | person/topic
  scope_tier        STRING NOT NULL,        -- proposed sharing scope (hierarchy)
  scope_audience    STRING NOT NULL,
  scope_sensitivity STRING NOT NULL,
  proposed_circles  ARRAY<STRING>,          -- proposed circle membership
  owner_unit        STRING,
  status            STRING NOT NULL         -- proposed|approved|rejected
)
PARTITION BY DATE(created_at);
