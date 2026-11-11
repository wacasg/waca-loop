-- ① 用語テーブル（terms） — §2.2 ①
-- <DS_GROUNDING> is substituted from the DS_GROUNDING env var by scripts/init_bigquery.py.
CREATE TABLE IF NOT EXISTS `<DS_GROUNDING>.terms` (
  term_id           STRING NOT NULL,        -- UUID
  term              STRING NOT NULL,        -- 表記
  reading           STRING,                 -- 読み/別表記（任意）
  definition        STRING NOT NULL,        -- 定義（人が書くWHY/扱い）
  basis             STRING,                 -- 根拠・扱い方
  attributes        JSON,                   -- AI付与（category, layer, aliases 等）。人が確定
  scope_tier        STRING NOT NULL,        -- personal|team|dept|org
  scope_audience    STRING NOT NULL,        -- internal|partner|public
  scope_sensitivity STRING NOT NULL,        -- normal|sensitive
  owner_unit        STRING,                 -- 所属組織単位（薄い参照）
  embedding         ARRAY<FLOAT64>,         -- 定義＋表記の埋め込み（VECTOR_SEARCH用）
  status            STRING NOT NULL,        -- approved|draft
  version           INT64 NOT NULL,
  created_at        TIMESTAMP NOT NULL,
  updated_at        TIMESTAMP NOT NULL
);
