-- ⑧ ポリシー行テーブル（AIポリシー・レベル制） — SPEC_phase1 §9
-- 人間が起案・承認する行（A〜I相当）。WACA Loopは読む（配達係）だけで、承認・書換えはしない（境界）。
-- <DS_FEEDBACK> is substituted from the DS_FEEDBACK env var by scripts/init_bigquery.py.
-- Dimension table (like terms) — no PARTITION BY.
-- Scope-axis waiver (§2.1): rows are internal/normal governance prose by design — no
-- audience/sensitivity columns yet. Add the axes BEFORE any sensitive/partner-visible
-- policy row is introduced.
-- 業務行は level(採用レベル 0/1/2)、下限行は level NULL + is_floor。
CREATE TABLE IF NOT EXISTS `<DS_FEEDBACK>.policy` (
  row_id      STRING NOT NULL,        -- 人が付ける行ID（例 KYO-6。ゼロ埋めしない）。安定キー
  scope_unit  STRING NOT NULL,        -- 適用先 owner_unit。org.toml の [units] org = 組織全体の床（§3.3）
  level       INT64,                  -- 採用レベル 0=未使用|1|2（業務行のみ。床行は NULL）
  line        STRING NOT NULL,        -- 「〜すること」対象の行為
  effect      STRING,                 -- 「どう効かせる/迂回するか」 床の迂回路 / 条件
  is_floor    BOOL   NOT NULL,        -- 絶対の🔴ブライトライン（下位で上書き不可）
  status      STRING NOT NULL,        -- approved|trial|draft（配達は approved/trial のみ）
  version     INT64  NOT NULL,
  trigger_kinds ARRAY<STRING>,        -- この行を発火させる data_kinds（§9.4。人が書きWACA Loopは読むだけ）
  created_at  TIMESTAMP NOT NULL,
  updated_at  TIMESTAMP NOT NULL
);
