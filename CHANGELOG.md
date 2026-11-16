# 変更履歴

本プロジェクトのバージョン番号は [Semantic Versioning](https://semver.org/lang/ja/) に従う。

## [1.1.0] - 2026-11-16

- 新ツール `export_my_usage` を追加。利用者が自分の AI 利用記録(台帳)だけを、一覧と CSV で
  書き出せる。
  - 対象は呼び出した本人の記録のみ。人を指定する引数は無く、他人の記録は出せない。
  - 要約のみ(会話の原文は台帳に無い)。取り下げた記録は `withdrawn=true` とし、要約を出さない。
  - 本人を確認できない利用者・組織の利用者でない場合は、理由つきで空の結果を返す。
  - 件数の上限 `_K_CAPS["export_my_usage"] = 500`。break-glass の対象外。
  - 呼び出しは `tool_events` に `usage_exported`(件数のみ)として残る。
  - 書き出すのは、いまのログインがその利用者に結び付いた時刻(`oauth_identity_map.claimed_at`)
    以降の記録だけ。メールアドレスが別の人に使い回されても、前の持ち主の
    記録は出ない。下限は返り値の `since` に入る。結び付け時刻が分からない場合は、スチュワードへの
    連絡を促す理由つきで空の結果を返す。
- 台帳ストアに `rows_for_actor` を追加(InMemory / BigQuery の双子)。結び付け時刻の床
  `not_before` を受け取る。
- 同じ結び付け時刻の床を `start_policy_workbench` と `request_log_deletion` にもかける
  (`ServerContext._owner_floor()` で1リクエスト1回だけ求めて共有)。
  - workbench: 前の持ち主の要約・log_id を使わない。結び付け時刻が不明なら要約なしで返し、
    `logs_withheld_reason` を付ける。`recent_summaries_for_actor` に `not_before` を追加(双子)。
  - 取り下げ: 床より前に起きた `actor_id` 一致の行は取り下げられない(「見つからない」と同じ答え)。
    結び付け時刻が不明なら理由つきで断る。`withdraw` に `actor_not_before` を追加(双子)。
  - coverage hint は床の対象外(回数のみ・毎ターンの照会を増やさないため。仕様 §4.1 で受容)。
- 人員ストアに `bound_at(subject, person_id)` を追加(InMemory / BigQuery の双子)。
- `Viewer` に `identity_mapped` を追加(識別マップで解決した viewer か)。

## [1.0.0] - 2026-11-11

最初の公開版。機能・導入方法は [README.md](README.md) を参照。

[1.1.0]: https://github.com/wacasg/waca-loop/releases/tag/v1.1.0
[1.0.0]: https://github.com/wacasg/waca-loop/releases/tag/v1.0.0
