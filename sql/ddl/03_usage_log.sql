-- ③ 層1 ガバナンス台帳（全件・軽い・要約JSON） — §2.2 ③
-- NOTE: no verbatim-prompt column by design — only `summary` (要約のみ / T6).
CREATE TABLE IF NOT EXISTS `<DS_LEDGER>.usage_log` (
  log_id        STRING NOT NULL,            -- UUID
  occurred_at   TIMESTAMP NOT NULL,
  actor_id      STRING,                     -- persons.person_id（解決できた場合）
  actor_raw     STRING,                     -- 解決不能時の識別（OAuth sub のハッシュ等）
  client        STRING NOT NULL,            -- claude_desktop|claude_code|chatgpt|gemini|other
  use_category  STRING NOT NULL,            -- l2|l1|l0|prohibited（利用区分。三色の入力は
                                          --   🟢→l2 / 🟡→l1 / 🔴→l0 に読み替える）
                                          -- 集計注意: 下限件数を数えるときは
                                          --   use_category 単独でなく policy_row_ids
                                          --   （policy.is_floor）と突き合わせること。
  data_kinds    ARRAY<STRING>,              -- pii|confidential|internal|public 等
  summary       STRING,                     -- モデルが生成した要点要約（逐語ではない）
  request_id    STRING,                     -- 学習リクエスト等との突合用（任意）。**1回の記録の
                                            --   再送キーでもある**:
                                            --   同 actor・同値・同内容の記録が15分以内にあれば
                                            --   新しい行を書かない。id 形以外は保存しない（自由文を
                                            --   この列に入れない — 内容列は summary だけ）
  ingested_via  STRING NOT NULL,            -- mcp|hook
  policy_row_ids ARRAY<STRING>,             -- 判断の根拠にしたポリシー行ID（§9.3、計器盤の入力）
  policy_version INT64,                     -- 適用時点のポリシー version（監査突合用）
  status        STRING,                     -- SPEC_ops R2: recorded(既定)|pending（組織帰属未確認・
                                            -- スチュワード審査待ち。破棄は行の物理削除）
  created_at    TIMESTAMP NOT NULL
)
PARTITION BY DATE(occurred_at);
-- 原文全文は層1に持たない（要約のみ）。パーティションの有効期限は設定しない（行は自動では
-- 消えない）。USAGE_LOG_RETENTION_DAYS は、全件を走査する処理が1回に読む期間の幅を決める。
-- R6 の取り下げはこの表を変更しない。ストリーミング挿入した直後の行は UPDATE できないため、
-- 取り下げは追記専用の `usage_withdrawals`（sql/ddl/14）で表し、読み取り側で除外する。
