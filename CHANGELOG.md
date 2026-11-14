# 変更履歴

本プロジェクトのバージョン番号は [Semantic Versioning](https://semver.org/lang/ja/) に従う。

## [1.1.0] - 2026-11-14

### 追加

- 新ツール `export_my_usage`。利用者が自分の AI 利用記録(台帳)だけを、一覧と CSV で書き出せる
  (仕様: [docs/SPEC_export_my_usage.md](docs/SPEC_export_my_usage.md))。
  - 対象は呼び出した本人の記録のみ。人を指定する引数は無く、他人の記録は出せない。
  - 要約のみ(会話の原文は台帳に無い)。取り下げた記録は `withdrawn=true` とし、要約を出さない。
  - 本人を確認できない利用者・組織の利用者でない場合は、理由つきで空の結果を返す。
  - 件数の上限 `_K_CAPS["export_my_usage"] = 500`、遡りは 365 日まで。break-glass の対象外。
  - CSV のセルが表計算ソフトで式として動かないよう、先頭の `=`・`+`・`-`・`@` 等を無害化する。
  - 呼び出しは `tool_events` に `usage_exported`(件数のみ)として残る。
- 台帳ストアに `rows_for_actor` を追加(InMemory / BigQuery の双子)。
- 人員ストアに `bound_at(subject, person_id)` を追加(InMemory / BigQuery の双子)。
- `Viewer` に `identity_mapped` を追加(識別マップで解決した viewer か)。

### セキュリティ

- 本人向けの台帳経路に「結び付け時刻の床」を導入した。person_id をメールアドレスから決める
  運用で、アドレスが別の人に使い回されて同じ person_id になっても、前の持ち主の記録を扱えない。
  床は `oauth_identity_map.claimed_at`(いまのログインが結び付いた時刻)で、
  `ServerContext._owner_floor()` が1リクエストにつき1回だけ求める。
  - `export_my_usage`: 床より前の記録を返さない。実際の下限を `since` で返す。
  - `start_policy_workbench`: 床より前の要約・log_id を使わない。`recent_summaries_for_actor` に
    `not_before` を追加(双子)。
  - `request_log_deletion`: 床より前に起きた行は取り下げられない(「見つからない」と同じ答え)。
    `withdraw` に `actor_not_before` を追加(双子)。
  - 識別マップで解決した利用者で結び付け時刻が分からない場合は、書き出し・取り下げを断り、
    workbench は要約なしで返す(`logs_withheld_reason`)。
  - coverage hint は床の対象外(回数のみ。毎ターンの照会を増やさないため。仕様 §4.1 で受容)。

## [1.0.0] - 2026-11-11

最初の公開版。機能・導入方法は [README.md](README.md) を参照。

[1.1.0]: https://github.com/wacasg/waca-loop/releases/tag/v1.1.0
[1.0.0]: https://github.com/wacasg/waca-loop/releases/tag/v1.0.0
