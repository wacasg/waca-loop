-- Source-layer envelope for ALL ingestion connectors (docs/ARCHITECTURE_knowledge_layers.md).
-- 知識の類型は増やさず、源泉が増えてもテーブルは増やさない — a new data source adds a
-- connector module, never a table. <DS_INTEGRATION> は
-- config.ds_integration（既定 waca_loop_integration）から scripts/init_bigquery.py が解決する。
-- New object → waca_loop_integration dataset only. Every text column has passed
-- src/waca_loop/ingest/anonymize.py (structural column-deletion first, then
-- deterministic scrub); no verbatim-BODY/customer-PII column exists by design.
-- title_text carries the RAW customer-authored subject line (scrubbed, but not a summary —
-- hence not *_summary); relax its scope only after a sampled human review. handled_by is a
-- person_id/slug mapped BEFORE the gate (担当者帰属は残す — never a raw display name;
-- unmapped names load as the literal 'unmapped').
CREATE TABLE IF NOT EXISTS `<DS_INTEGRATION>.source_items` (
  source STRING NOT NULL,              -- connector id, e.g. 'source_a' | 'source_b'
  source_id STRING NOT NULL,           -- source-system key (reverse lookup happens in the source system)
  occurred_at TIMESTAMP,               -- when the item happened (source system's creation time)
  title_text STRING,                   -- gate-scrubbed RAW short text (see header)
  body_summary STRING,                 -- gate-scrubbed body summary
  resolution_summary STRING,           -- gate-scrubbed resolution note
  handled_by STRING,                   -- person_id/slug ('' = none, 'unmapped' = name not in map);
                                       -- feeds ERN signals AFTER canonical-layer adoption, never
                                       -- directly from this table (ARCHITECTURE_knowledge_layers「ERN 連携の原則」)
  attrs JSON,                          -- connector-specific fields (e.g. request_id/closed_at/
                                       -- category/status/channel/priority). New connector
                                       -- columns land here — no DDL change, no new table
  route STRING,                        -- faq | sfaq | terms | hold_external | discard
                                       -- 初期ロードでは NULL。hold_external は外部の事例エンジン
                                       -- へ渡す候補の保留
  route_confidence STRING,             -- high | low  (low → human review queue)
  routed_by STRING,                    -- 'ai' or a role SLUG — never a personal name
  scope_tier STRING NOT NULL,          -- ACL axis 1/3 (§3.3); load default 'dept' — narrow first.
  scope_audience STRING NOT NULL,      -- ACL axis 2/3 (§3.3); loader pins 'internal'
  scope_sensitivity STRING NOT NULL,   -- ACL axis 3/3 (§3.3); loader pins 'sensitive' —
                                       -- masked customer-derived text is still re-identifiable
                                       -- (relax only after a sampled human review)
  owner_unit STRING NOT NULL,          -- ownership: unit that owns/serves these items
  gate_version STRING,                 -- anonymize detector + connector + slice provenance (audit)
  loaded_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(occurred_at)
CLUSTER BY source, route;
-- ローダーの約束: 冪等にする（キー = source, source_id, owner_unit で MERGE）。routing 3列は
-- 人・AI の判断の列なので再ロードで上書きしない（内容が変わった行は route_confidence を
-- 'low' に下げて再レビューへ回す）。scope は internal/sensitive/dept の狭い既定から始め、
-- owner_unit はロードのたびに明示する（推測しない）。
