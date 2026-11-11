-- ツールイベント証跡（SPEC_ops R3a — 記録カバレッジ監視の観測点）
-- policy_check / usage_recorded / scope_declared / scope_confirmation_requested /
-- usage_reviewed / retrieval の薄い行のみ。内容は一切持たない（detail はスコープ値と
-- 「操作:log_id」のみ。reason_summary・組織名・要約は保存しない）。
-- usage_reviewed の discard は usage_log の行を物理削除するため、この表が「保留行が存在し、
-- 誰がいつ破棄したか」を残す唯一の場所。
CREATE TABLE IF NOT EXISTS `<DS_LEDGER>.tool_events` (
  event_id     STRING NOT NULL,             -- UUID
  occurred_at  TIMESTAMP NOT NULL,
  tool         STRING NOT NULL,             -- policy_check|usage_recorded|scope_declared|
                                            --   scope_confirmation_requested|usage_reviewed|
                                            --   retrieval
  actor_id     STRING,                      -- persons.person_id（解決できた場合）
  actor_raw    STRING,                      -- 解決不能時のハッシュ済み識別（§5 と同じ流儀）
  detail       STRING,                      -- scope_declared: org|external|private|unknown
                                            -- usage_reviewed: (approve|discard):<log_id>
                                            -- 他は NULL。自由文は入れない
  created_at   TIMESTAMP NOT NULL
)
PARTITION BY DATE(occurred_at);
-- 集計は scripts/coverage_report.py（policy_check − usage_recorded = 不整合疑い件数）。
-- R3b: retrieval は読み取り系ツールの薄い証跡（ツール名のみ・クエリは持たない）。検索したのに
-- 記録が1件も無い actor/日 = silent を出すためにある。policy_check も log_usage も呼ばない
-- クライアントは両カウンタが0で不整合も0になり、不整合の指標だけでは**正常に見えてしまう**。
