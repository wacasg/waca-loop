-- ERN（専門度・貢献量・信頼度エンジン）— SPEC_phase3 §1・§2.1
--
-- 増やすのは「宣言」と「監査」の2つだけ。**シグナルそのものは新テーブルを作らない** —
-- 専門度・貢献量は `<DS_KNOWLEDGE>.feedback` の既存行（fb_type × created_at × author_id）
-- からの導出値である（SPEC_phase3 §1）。
--
-- E3: この2表に rank / score / rating / order 列を**置かない**。順位や総合点をテーブルに
-- 残すと、ERN が「評価ではなくマッチング」（INV-5）であるという前提が、データ側から崩れる。
-- tests/test_e03_no_score_columns.py がこのファイルを直接 grep して表明する。
--
-- E8: 逐語を持たない。prompt / raw_text / transcript / body 相当の列は無い（INV-1）。

-- ① 人 × 領域の宣言／導出プロファイル（SPEC_phase3 §2.1）
CREATE TABLE IF NOT EXISTS `<DS_KNOWLEDGE>.ern_assertions` (
  assertion_id        STRING NOT NULL,      -- UUID
  person_id           STRING NOT NULL,      -- <DS_GROUNDING>.persons.person_id
  topic               STRING NOT NULL,      -- 領域ラベル（正規化済み）
  embedding           ARRAY<FLOAT64>,       -- リコール専用。スコアには一切寄与しない
                                            --   （"no vectors for scoring"）
  source_candidate_id STRING,               -- 由来（extraction_candidates 等）
  status              STRING NOT NULL,      -- declared|derived|frozen
  created_at          TIMESTAMP NOT NULL,
  updated_at          TIMESTAMP NOT NULL
)
CLUSTER BY person_id, topic
OPTIONS(description="ERN: 人×領域の宣言/導出プロファイル。順位・総合点は持たない (SPEC_phase3 §2.1)");

-- ② append-only 監査証跡（SPEC_phase3 §2.3 ③信頼度）
--
-- 「いつ・誰が・何を根拠に候補になったか」を後から再現できることを信頼度と定義する。
-- **数値化しない。** この表を読み出す MCP ツールは作らない（SPEC_phase3 §4f）— 作ると
-- 「誰が誰を探したか」が読め、E6 のサイレント非対称開示が破れる。
--
-- E7: append-only。update / delete の API を作らない。同じ検索を2回すれば2行になる。
CREATE TABLE IF NOT EXISTS `<DS_FEEDBACK>.ern_audit` (
  audit_id       STRING NOT NULL,           -- UUID
  occurred_at    TIMESTAMP NOT NULL,
  actor_id       STRING,                    -- 探した人。persons.person_id（解決できた場合）
  actor_raw      STRING,                    -- 解決不能時のハッシュ済み識別（§5 と同じ流儀）
  topic          STRING,                    -- 探した領域ラベル。**問い合わせ本文ではない**
  candidate_ids  ARRAY<STRING>,             -- 候補になった person_id（この表にだけ残る）
  evidence_types ARRAY<STRING>,             -- 実績ベース|役職ベース（候補と同順）
  created_at     TIMESTAMP NOT NULL
)
PARTITION BY DATE(occurred_at)
CLUSTER BY actor_id
OPTIONS(
  description="ERN: append-only 監査証跡。読み出す MCP ツールを作らない (SPEC_phase3 §4f)",
  -- **これがこのシステムで最も機微な表**（誰が誰を探したか）。無期限に置かない。
  -- 365 日で消す（`usage_log` は自動では消さないが、この表は最も機微なので期限を持つ）。
  -- `config.ern_audit_retention_days` と揃える。
  --
  -- 注意: パーティション期限は行そのものを消すが、**SPEC_phase3 §5 の purge で個人を消したときに
  -- その person_id が他人の candidate_ids から消えるわけではない**。そこは未解決
  -- (SPEC_phase3 §7)。
  partition_expiration_days=365
);
