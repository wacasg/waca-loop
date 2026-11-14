# SPEC 自分の利用記録の書き出し（export_my_usage）

- 実装: v1.1.0（`src/waca_loop/server.py` の `export_my_usage`・`ledger/usage_log.py` の `rows_for_actor`）
- 状態: v1.1.0 で実装済み
- 関連: `docs/SPEC_phase1.md`（§2.2③ 台帳は要約のみ・§3.3 ACL・§5 帰属）、`docs/ACCESS_MATRIX.md`

## 1. 目的

利用者（研修・講座の受講生を含む）が AI クライアントに「自分のログを出力して」と頼むと、
WACA Loop が **その人自身の利用記録だけ** を返す。研修での振り返りや、自分の使い方の確認に使う。

**他の人の記録は、どんな引数・どんなログイン状態でも返さない。** これがこの機能の要件の
すべてに優先する。

## 2. ツール

`export_my_usage(days: int = 30, limit: int = 200) -> dict`

- **人を指定する引数を持たない。** 対象は、ログインで確かめた本人（`viewer.person_id`）だけ。
  引数で person_id・actor・メールアドレスを受け取る形には、将来も変えない。
- `days`: さかのぼる日数。1〜365 に丸める。
- 既定は 30日・200件。AI クライアントは、呼ぶ前に期間と件数を利用者に聞いてよく、
  結果の `csv` を利用者の求めに応じてファイルとして保存・提供してよい（ツール説明に明記）。
- `limit`: 返す件数。`_clamp_k(limit, "export_my_usage")` を通す。上限 500（`_K_CAPS` の既定値。変更は導入組織の判断で）。
  break-glass（`export_admin_units`）でも上げられない（`_NO_BREAK_GLASS`）。

## 3. 返り値

| キー | 中身 |
|---|---|
| `actor` | 本人の person_id |
| `count` | 返した件数 |
| `rows` | 記録の配列（下表） |
| `csv` | `rows` と同じ内容の CSV（UTF-8・見出し行つき。配列は `;` でつなぐ） |
| `notice` | 「本人の記録のみ」の注記 |
| `days` / `limit` | 実際に使った値 |
| `since` | 実際に使った下限の日時（ISO 8601・UTC）。`max(いま − days, 結び付け時刻)`（§4.1） |

`rows` の1件:

| 項目 | 中身 |
|---|---|
| `log_id` | 記録の ID |
| `occurred_at` | 日時 |
| `client` | 使った AI クライアント |
| `use_category` | l2 / l1 / l0 / prohibited |
| `data_kinds` | 扱ったデータの種類 |
| `summary` | 要約（取り下げた記録は出さない） |
| `policy_row_ids` | 該当したポリシー行 |
| `withdrawn` | 取り下げ済みか |

**逐語のプロンプト・会話は返さない。** 台帳にもともと無い（§2.2③、T6）。

## 4. 本人以外を出さない仕組み（二重）

1. **保管側**: `rows_for_actor(actor_id, *, days, limit, now=None, not_before=None)` を
   InMemory と BigQuery の両方に持つ。BigQuery は `actor_id = @actor_id AND occurred_at >= @since`
   （パラメータ束縛・パーティション列で期間を絞る）で、許可した列だけを SELECT する。
   `@since` には §4.1 の結び付け時刻の床も畳み込む。
2. **サーバー側**: 返ってきた行を、もう一度 `actor_id == viewer.person_id` と
   `occurred_at >= since` で絞ってから返す。

どちらか一方が壊れても、もう一方で止まる（§3.3 の「ACL は二度かける」と同じ考え方）。

### 4.1 結び付け時刻より前の記録は出さない

**背景.** 研修・講座の受講生のように、person_id をメールアドレスから決めて登録する運用が
ある（アドレスのハッシュから ID を作る）。登録を外しても台帳は残す。同じアドレスが後で
別の人に渡り、その人が登録されると **同じ person_id** になり、何もしなければ前の人の記録が
書き出される。

**決定.** 書き出すのは、**いまのログイン（`sub`）がその person_id に結び付いた時刻以降に
起きた記録だけ** とする。

- 結び付け時刻は `oauth_identity_map.claimed_at`。事前登録の行が本人の初回ログインで
  束ねられたときに入る（`claim_pending_registration`）。
- 引くのは **いまの検証済み sub と person_id の組** の行だけ（`bound_at(subject, person_id)`、
  InMemory / BigQuery の双子。BigQuery は両方をパラメータ束縛）。「その person_id の
  どれかの行」では引かない — 前の持ち主の古い結び付け時刻を借りてしまうため。
  同じ組の行が複数あり、どれか1つでも `claimed_at` が NULL なら「不明」、そうでなければ
  最も新しい時刻（厳しい側）を使う。
- 下限は `since = max(いま − days, 結び付け時刻)`。保管側（`@since`）とサーバー側の両方で
  かける（§4 の二重化）。返り値の `since` に実際の値を入れ、`notice` に
  「結び付けた時点より前の記録は含みません」と書く。
- 床は `ServerContext._owner_floor()` が求め、`(since|None, 断る理由|None)` を返す。
  1リクエスト（=1コンテキスト）につき引き当ては高々1回で、結果はコンテキストに保持する。
  全ツールのリクエストごとに BigQuery を1回増やさないため、Viewer には「識別マップで
  解決したか」（`identity_mapped`）だけを載せ、時刻そのものは載せない。

**床をかける経路（本人向けの台帳経路すべて）.**

| 経路 | 床の効き方 | 結び付け時刻が不明なとき |
|---|---|---|
| `export_my_usage` | `rows_for_actor(..., not_before=)`。保管側 `@since`＋サーバー側の再確認 | 空の結果＋理由（`refused=bind_unknown`） |
| `start_policy_workbench` | `recent_summaries_for_actor(..., not_before=)`。BigQuery は `l.occurred_at >= @since`（パラメータ束縛）、InMemory も同じ規則、さらに workbench 内で再確認。`current_state` の要約と `source_log_ids` に前の持ち主の行が出ない | ツール自体は返す（方針の案内は出す）が、**要約も log_id も使わない**。`logs_withheld_reason` に理由 |
| `request_log_deletion` | `withdraw(..., actor_not_before=)`。`actor_id` で一致した行は `occurred_at >= 床` のときだけ本人の行とみなす（BigQuery は INSERT…SELECT の WHERE 内、InMemory は `_owns`）。床より前の行は「見つからない」と同じ答え（存在の探りに使わせない）。`actor_raw`／`subject_hash` で一致する行（同じ Google sub が結び付け前に残した本人の行）は床の対象外 | 理由つきで断る（`usage_withdraw_refused` を残す） |
| coverage hint（`get_applicable_policy` の `coverage_hint`） | **かけない（受容）** | — |

`recent_summaries_for_actor` を本番で呼ぶのは workbench だけである。

**coverage hint を床の対象外にする理由（受容したリスク）.** ヒントは毎ターンの
`get_applicable_policy` で計算するので、床をかけるには毎ターン BigQuery の照会が1回増える。
漏れうるのは「直近7日（`RECENT_WINDOW_DAYS`）の自分の記録漏れの**回数**を一行で」だけで、
要約・log_id・日時は返らない。再登録の直後7日間に限り、前の持ち主の記録漏れ回数が
ヒントに混ざりうる。害が小さく、コストが毎ターンかかるので受容する。

**結び付け時刻が分からないとき（fail closed）.**

| viewer の出どころ | 結び付け時刻 | 振る舞い |
|---|---|---|
| `resolve_viewer`（識別マップで解決。ホスト版・stdio 版の両方） | 記録あり | 床を適用して返す |
| 同上 | 不明（NULL・列追加前の行・管理者が sub を直接入れた行・ストア未配線・照会の失敗） | **空の結果＋理由**（「運営担当（スチュワード）に連絡してください」）。`refused=bind_unknown` を残す |
| プロセス内で直接組み立てた viewer（テスト・オフライン開発。`identity_mapped=False`） | 問う先が無い | 日数の窓だけで返す（床なし） |

理由: 本番の入口は2つ（`build_request_context_factory` と `build_production_context`）とも
`resolve_viewer` を通り、そこで必ず `identity_mapped=True` になる。したがって本番で床を
飛ばす経路は無い。直接組み立てた viewer は、他のツールでもそのまま信頼されている
テスト用・開発用の viewer であり、識別マップそのものが無いので結び付け時刻を問えない。
ホスト版で時刻が不明なときに「床なしで返す」を選ぶと、まさに使い回しの事故が起きる行
（管理者が sub を入れ直した行など）で前の人の記録が出るので、断る側に倒す。

**運用.** 断られた人は、スチュワードが該当行の `claimed_at` を（確認のうえ）設定し
`claim_source = 'admin'` とすれば書き出せるようになる。設定した時刻より前の記録は出ない。

**運用ルール（必須）: 識別マップの行を手で別の sub に付け替えるとき.** `oauth_sub` を
書き換えて行を新しい sub に向け直す場合は、**同じ更新で `claimed_at` を付け替えた時刻に
する（または NULL にする）**。前の持ち主の `claimed_at` を残してはならない。`bound_at` は
「いまの sub とその person_id の行の `claimed_at`」を読むので、古い時刻が残っていると
新しい sub に前の持ち主の記録まで含む床が与えられ、この節の防御が外れる。NULL にした
場合は本人が書き出し・取り下げを断られ、スチュワードへの連絡で改めて時刻を設定する。

**未決（スキーマ変更の承認が必要）.** 上の運用ルールは人手に頼っている。より強い対策は、
結び付けた時点の sub のハッシュ（`claimed_sub_hash`）を `claim_pending_registration` で
同時に記録し、`bound_at` では「`claimed_sub_hash` がいまの sub のハッシュと一致する行」の
`claimed_at` だけを使うこと。手で sub を付け替えても古い時刻が新しい sub に効かなくなる。
`oauth_identity_map` への列追加（ALTER）を伴うため、導入組織の判断を待つ。

**引き換えに失うもの.** 同じ本人が登録を外して再登録した場合も、再登録より前の記録は
書き出せなくなる。本人確認の手段が「同じアドレスでログインできること」しか無い以上、
本人と別人を見分けられないので、出さない側を選ぶ。

## 5. 断る場合

| 状態 | 返すもの |
|---|---|
| person_id が無い（未登録・未解決のログイン） | 空の結果＋日本語の理由 |
| 組織のメンバーでない | 空の結果＋日本語の理由 |
| 識別マップで解決したが、いまのログインの結び付け時刻が不明（§4.1） | 空の結果＋日本語の理由（スチュワードへの連絡を促す） |

ハッシュ化した `actor_raw` などに切り替えて探すことはしない。

## 6. 取り下げた記録

`withdrawn: true` を付けて行は返すが、**要約は返さない**。BigQuery では SQL の中で
要約を NULL にするので、取り下げた文面はデータベースの外に出ない。

## 7. 監査の記録

呼ばれるたびに `tool_events` に `usage_exported` を残す（失敗しても書き出しは止めない）。
中身は件数だけ（`days=<d>:k=<k>:n=<n>`、断ったときは `refused=unresolved` /
`refused=not_org_member` / `refused=bind_unknown`）。取り下げの床による拒否は、取り下げの
既存の記録（`usage_withdraw_refused`）に残る。書き出した記録の中身は残さない。

## 8. 受け入れ条件（tests/test_export_my_usage.py）

- A が呼ぶと A の記録だけが返る。B の記録が新しくても、A に記録が無くても、B の記録は出ない
- 未解決のログインは空＋理由
- 取り下げた記録は印がつき、要約が無い
- `limit` が上限で丸められる／`days` の範囲で絞られる
- CSV の見出しと件数が `rows` と一致する
- ツールの引数に人を指定するものが無い
- InMemory と BigQuery が同じ行を返す
- 監査の記録が件数だけ
- 同じ person_id の記録でも、いまのログインの結び付け時刻より前のものは出ない（§4.1）。
  アドレスを使い回した後任者に、前の持ち主の記録が出ない
- 識別マップで解決した viewer で結び付け時刻が不明・照会に失敗 → 空＋理由
- `bound_at` は sub と person_id の組で引く（別の sub・別の person では引けない）。
  BigQuery 側はパラメータ束縛で、NULL を不明として返す
- 床の適用（`not_before`）で InMemory と BigQuery が同じ行を返す
- （tests/test_owner_bind_floor.py）使い回しの後任者に対して、workbench の要約・`source_log_ids`
  に前の持ち主の行が出ない。結び付け時刻が不明なら workbench は要約なしで返り、
  `logs_withheld_reason` を付ける
- 使い回しの後任者は前の持ち主の行を取り下げられない（「見つからない」と同じ答え）。
  自分の行は取り下げられる。結び付け前に同じ sub のハッシュで残った本人の行も取り下げられる。
  結び付け時刻が不明なら理由つきで断る。InMemory と BigQuery で同じ結果
- 床の引き当ては1リクエストにつき1回
- 本番の入口（`build_request_context_factory`・`build_production_context`）が作る viewer は
  `identity_mapped=True`

## 9. 決定事項

1. 既定 30日・200件、上限 365日・500件で確定。期間と件数は利用者に聞いてファイルを作ってよい
2. ログインが紐づく前に、ハッシュ化した ID だけで残った記録は **含めない**
3. 取り下げた記録の要約は、本人にも **見せない**
4. 組織のメンバーでない人の書き出しは **断る**
5. 書き出しは、いまのログインがその person_id に結び付いた時刻以降の記録に **限る**。
   結び付け時刻が不明なら断る（§4.1）。同じ床を workbench と取り下げにもかける

## 10. 既知のリスク（未決）

- coverage hint は床の対象外（§4.1 で受容）。手での sub 付け替えは運用ルール頼みで、
  `claimed_sub_hash` による恒久対策はスキーマ変更を伴うため未決（§4.1）。
