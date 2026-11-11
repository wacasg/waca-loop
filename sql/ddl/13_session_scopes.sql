-- ⑬ セッションスコープ宣言 — 再起動をまたいで残す
--
-- なぜ永続化するか。宣言をプロセス常駐のインメモリ辞書だけで持つと、
-- **デプロイやコールドスタートで全員の宣言が unknown に戻る** — 「業務外」と宣言した
-- 会話が、本人が何もしていないのに記録される側へ戻り得る fail-open になる。ここに落とせば、失効は TTL という**ひとつの
-- 予測できる規則**だけになる。
--
-- **生の OAuth sub は持たない** (§5)。インメモリ側は viewer.subject を鍵にするが、それは
-- プロセス内に閉じているから許される話で、保存するなら ledger と同じ分け方 —
-- 解決済みは actor_id、未解決はハッシュ済みの actor_raw — に従う。
--
-- **理由は持たない** (R1)。reason_summary は宣言時にも保存していない。除外の指標は件数のみで、
-- 内容も組織名も残さない。ここに置くのは「誰が・どの scope を・いつ」だけ。
--
-- **追記のみ・最新を読む**。現在値テーブルにしないのは、BigQuery は streaming buffer の行に
-- 対する DML を拒む（~30〜90分）。そして**その窓の中で宣言し直すのが普通の使い方**で、拒否文
-- 自身が「宣言し直してください」と案内している。DELETE+INSERT だと DELETE が落ちて INSERT が
-- 走らず、ストアには**古い方の scope**が残る — 09:10 に org、09:25 に private、10:00 に再起動で
-- 除外が消える。追記なら、書き込みの
-- 失敗は「前の行が残る」= 締まる側に倒れる。
--
-- **保持：既定90日**（`usage_log` は自動では消さない）。行の中身は「この人がこの時刻に業務外だと
-- 宣言した」という、人に貼られるラベルとして読める種類の記録で、失効した行はもう何の効力も
-- 持たない。期限で落ちることが唯一の消滅経路である。



CREATE TABLE IF NOT EXISTS `<DS_LEDGER>.session_scopes` (
  actor_id     STRING,                     -- 解決済みの person_id（§5）
  actor_raw    STRING,                     -- 未解決はハッシュ済み subject。生 sub は入れない（§5）
  scope        STRING NOT NULL,            -- org | external | private | unknown
  declared_at  TIMESTAMP NOT NULL,         -- TTL の起点。宣言し直すたびに更新される
  updated_at   TIMESTAMP NOT NULL
)
PARTITION BY DATE(declared_at)
OPTIONS (
  partition_expiration_days = 90,
  description = "セッションスコープ宣言の履歴（追記のみ・最新を読む）。R1・保持90日"
);
