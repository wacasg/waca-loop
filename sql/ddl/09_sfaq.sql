-- ⑨ SFAQ — 判断ナレッジ層（SPEC_phase2 §2）
-- <DS_KNOWLEDGE> is substituted from the DS_KNOWLEDGE env var by scripts/init_bigquery.py.
-- questions は次元テーブル（terms と同様・no PARTITION BY）。feedback は追記のみ。
-- NOTE: owner_unit はスコープ3軸ACL（§3.3）の personal/dept ゲートに必須
-- （terms.owner_unit と同じ役割）。

-- 問い（中心オブジェクト）
CREATE TABLE IF NOT EXISTS `<DS_KNOWLEDGE>.questions` (
  question_id   STRING NOT NULL,
  question_text STRING NOT NULL,           -- 「観察された課題」から生成（Q1a）
  attributes    JSON,                       -- 型/分類はAIが自動判定（Q3a）
  cluster_id    STRING,                     -- 重複統合の束ね先（Q4）
  auto_merge    BOOL NOT NULL,              -- このクラスタの自動統合フラグ（Q4・既定false / S6）
  provenance    STRING NOT NULL,            -- corporate_training|individual|other（Q6）
  owner_unit    STRING,                     -- ACL: personal/dept tier のゲート（§3.3）
  scope_tier    STRING NOT NULL,            -- personal を既定に（S2）
  scope_audience STRING NOT NULL,           -- internal|partner|public
  scope_sensitivity STRING NOT NULL,
  perm_corporate BOOL,                      -- 法人パーミッション（Q6）
  perm_individual BOOL NOT NULL,            -- 個人パーミッション（Q6・既定false / S2）
  status        STRING NOT NULL,            -- candidate|provisional|approved|contested（S4）
  source        STRING,                     -- 取り込み元の識別子（例 migration / master_import）
  embed_text    STRING,                     -- 検索が見る文字列（見出し＋本文）— 下記
  embedding     ARRAY<FLOAT64>,             -- 問い⇄問いマッチング（Vertex / S9）
  created_at    TIMESTAMP NOT NULL,
  updated_at    TIMESTAMP NOT NULL
);

-- フィードバック（問いに積もる）
CREATE TABLE IF NOT EXISTS `<DS_KNOWLEDGE>.feedback` (
  feedback_id   STRING NOT NULL,
  question_id   STRING NOT NULL,
  fb_type       STRING NOT NULL,            -- 6型: decision|agree|refute|supplement|open|useful（S5）
  body          STRING,                     -- 施策/結果/学び/コメント等
  author_id     STRING,                     -- E-3(ii) 該当時は配信時に伏せる
  created_at    TIMESTAMP NOT NULL
);

-- embed_text — 検索が見る文字列
--
-- question_text は「見出し」であって組織の学びそのものではない。学びの中身は
-- feedback(decision) に入っている。見出しだけを埋め込むと、検索の当たり外れが見出しを
-- 作った正規表現の出来に丸ごと依存する（規則が当たらなかった行は
-- 「〜である。— このとき、どう判断するか？」のような見出しになる）。
--
-- embed_text に「見出し＋本文」を持たせ、字面検索もベクトル検索もこちらを見る。文体の
-- 揺らぎはベクトルが吸収する（正規表現では吸収しない）。見出しは
-- 派生物になり、規則を直して作り直せる（コーパスを触らずに品質を上げられる）。
--
-- 埋め込む文字列を JOIN で都度組み立てず**列として持つ**のは、何を埋め込んだかが SELECT で
-- 見えるようにするため。検索がおかしいときに原因を追えないと、件数が増えた後で詰む。
--
-- embed_text が NULL の行は question_text を埋め込み対象にする。作り直すときは
-- `python scripts/seed_sfaq.py --rebuild-embed-text` → `--backfill-embeddings` の順に実行する。
