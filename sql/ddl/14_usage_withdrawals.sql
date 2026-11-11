-- ⑭ R6 取り下げ — 台帳行の「取り下げ」を**追記だけ**で表す
--
-- なぜ usage_log に列を足さないか:
--   usage_log は `insert_rows_json`（ストリーミング挿入）で書いている。BigQuery は
--   **ストリーミングバッファに残っている行（およそ30〜90分）への UPDATE/DELETE を拒否する**。
--   R6 が使われるのは「いまAIが私事を記録してしまった」直後 — つまり**必ずバッファの中**。
--   `deleted_at` 列を UPDATE で立てる設計は、いちばん必要な場面でだけ失敗する。しかも
--   本番でしか起きないのでオフラインのテストは緑のまま通る。
--   追記なら制約を受けないので、**取り下げは即座に効く**。読み取り側がこの表を引いて外す。
--
-- 取り下げは取り消せない（この表から行を消す経路は無い）。「物理削除しない」は
-- usage_log 側の話で、要約はそのまま残る — 見えなくなるだけ。
CREATE TABLE IF NOT EXISTS `<DS_LEDGER>.usage_withdrawals` (
  withdrawal_id STRING NOT NULL,            -- UUID
  log_id        STRING NOT NULL,            -- 取り下げた usage_log.log_id
  actor_id      STRING,                     -- 申告者＝その行の持ち主（persons.person_id）
  actor_raw     STRING,                     -- 解決不能時のハッシュ済み識別（§5 と同じ流儀）
  reason        STRING NOT NULL,            -- private|other_org|mistake（閉じた符号。
                                            --   自由文にしない — 取り下げたい内容を
                                            --   同じ台帳に書き写させないため / FLOOR-01）
  withdrawn_at  TIMESTAMP NOT NULL,
  created_at    TIMESTAMP NOT NULL
)
PARTITION BY DATE(withdrawn_at);
-- 内容は持たない — log_id と符号だけ。
-- **保持の不変条件**: この表は `usage_log` より**先に消えてはならない**。取り下げ行が先に
-- 消えると、隠していた要約がその瞬間に全部よみがえる（anti-join が空振りするため）。
-- 数字を合わせるのではなく、この向きを守ること。`withdrawn_at >= occurred_at` は常に成り立つ
-- ので、同じ保持期間なら安全側に倒れる。短くするのは不可。
