# WACA Loop MCPサーバー運用仕様書(WACA Loop ops)

- 実装: `src/waca_loop/guard.py`・`ledger/tool_events.py`・`server.py`(SPEC_ops 参照コメント)

## 1. 問題提起

「毎プロンプトで log_usage を呼ぶ」ことをツール説明文で指示しても、遵守は確率的で欠落が起きる。原因:

1. **ツール説明文には強制力がない**(description は参考情報。WACA Loop と無関係に見えるターンでスキップされる)
2. **クライアント側のツール遅延ロード**(認識されないターンでは説明文自体が視界に入らない)
3. **MCP 仕様上、サーバーが応答出力に介入するフックがない**
4. **マルチ組織問題**: 他組織・私事の内容が組織の台帳・SFAQ・用語層に記録されると、
   守秘・個人情報・組織間信頼の重大な問題になる

## 2. 目的(Goals)

- **G1 記録カバレッジの可視化**: log_usage の「呼ばれなかったターン」を検出可能にし、記録率を計測する
- **G2 組織スコープ除外**: 組織の業務外の会話をログ収集・情報共有から確実に除外する(誤記録報告ゼロ)
- **G3 二重防御**: サーバー側ガードとクライアント側指示の両輪

## 3. 非目的

MCP プロトコル拡張はしない / 逐語ログは収集しない(要約のみを維持する) /
マルチテナント化は本版の範囲外 / クライアント側の挙動改修は対象外

## 4. ユーザーストーリー(要約)

利用者=自動記録でポリシー遵守を証明したい / マルチ組織利用者=組織外の内容が残らない保証と誤記録の削除 /
スチュワード=欠落率の把握と起票 / 管理者=除外判定の最小メタデータ監査(除外内容自体は見えない)

## 5. 要件と実装状態

### P0(実装済み)

**R1 `set_session_scope`** — scope(org|external|private|unknown)+reason_summary。
external/private の宣言後は、log_usage / propose_candidates / add_sfaq_* / submit_survey_response を
サーバー側で受理拒否する(no-op)。
実装注記: 宣言は会話単位ではなく **利用者単位** で効く(Streamable HTTP のセッションIDはツール本体に
渡らないため。鍵と寿命は次項)。reason_summary は**保存しない**
(除外メトリクスは件数のみ — `SessionScopeRegistry.excluded_declarations` と tool_events の
scope_declared 行。内容・組織名は持たない)。scope 未宣言のセッションは記録する。

**R1 の寿命 — 永続化と TTL**。宣言は `viewer.subject` ではなく **§5 の `(actor_id, actor_raw)`** を鍵に
`session_scopes` テーブルへ保存され、**プロセスをまたいで残る**。宣言を終わらせるものは**2つだけ**である。

1. 宣言し直す
2. **TTL の失効**(`SCOPE_TTL_HOURS`・既定12時間)

永続化するのは、再起動やデプロイのたびに宣言が `unknown` へ戻ると、`private` と宣言した会話が本人の
操作なしに記録される側へ戻り得る(fail-open)ためである。永続化により、失効を**ひとつの予測できる規則**にする。

12時間の根拠: 朝に宣言して夕方まで効き、**夜をまたがない**。戻し忘れは「別の日・別の会話で記録が落ちる」
形で現れるので、そこで切れるのが要点である。`SCOPE_TTL_HOURS=0` で自動失効を無効にし、宣言し直すまで
永続させることもできる。

**失効の失敗方向は高くつく**(まだ業務外の会話で失効すると私事が台帳に入る)。R6(`request_log_deletion`)で
取り返しはつくが、**本人がその行に気づいて取り下げる**必要があるので、黙って起きてよい種類の事故ではない。
手当ては3つ:

- **時刻を持たない宣言は失効させない**。古さが分からないものを「古い」と扱わない
- **失効の約2時間前に警告**する(`scope_expiry_warning`)
- **失効した瞬間に1回だけ知らせる**(`scope_lapsed`)。事前警告は寝ている間に過ぎうるし、
  `get_applicable_policy` を呼ばないクライアントには届かない。再開する側も名乗る

**ストアの失敗方向**。書き込み失敗はプロセス内の宣言を有効なままにする(締まる側)。読み取り失敗は
**締められない** — その actor に宣言があったかどうかが分からず、一過性の 503 で全員の書き込みを止める
ほうが害が大きい。よって fail-open に degrade し、R1b の1件ごとの内容ゲートが受け皿になる。ただし
**黙らない**: `store_read_failures` / `store_write_failures` を数え、警告を記録する。**DDL 未適用の本番は、
この数字が上がることでしか区別できない。**

**保存するもの**は「誰が・どの scope を・いつ」だけである。理由(`reason_summary`)は宣言時にも保存しない。
テーブルは**追記のみ・最新を読む**(BigQuery が streaming buffer の行への DML を拒み、その窓の中で
宣言し直すのが普通の使い方であるため。DELETE+INSERT では古い方の scope が残る)。
**保持は90日**(`usage_log` は自動では消さない。失効した宣言の行は効力を持たないため短くしている)。

**R1b は scope で免除しない**。`org` は external/private と**同じだけ常駐する**ので、宣言で免除すると、
一度 `org` を宣言した人の R1b が以後ずっと無効になり、私事の要約が台帳に入り得る。R1b が訊いているのは
「この人は何をしている人か」ではなく「**この1件の中身**を台帳に入れてよいか」なので、答えになるのは
人単位の宣言ではなく、その1件が組織の業務である証拠(`org_context` に組織との関連 / `policy_row_ids`)
である。案内文もそちらを指す。

**R1c 立っている宣言の先出し**。拒否は最初の**書き込み**まで出ないので、それまでのターンは黙って落ちる。
`get_applicable_policy` の応答に、除外が立っているときだけ `scope_notice` を1行付ける
(`ServerContext._standing_scope_notice`)。自分の分のみ・助言のみ・失敗しても本体は返る(C4 と同じ制約)。
ACL が行を1つも返さない部外者には**付けない** — 規約を教えない相手に、こちらが人ごとの scope 台帳を
持っていることを明かさないため。

**R1d 失効は再開しない**。TTL は scope によらず一律12時間とし、**非対称化は scope の長さではなく
失効の意味の側で行う**。

失効すると宣言の**効力**は切れる(`scope_of` は `unknown` を返す)が、**書き込みは拒否のまま**残る
(`SessionScopeRegistry.is_lapsed` / `writes_blocked`)。再開の経路は時間の経過ではなく、
`set_session_scope` の呼び直し1回だけ — 組織の業務なら `org`、まだ業務外なら同じ scope。
後者は失効状態を普通の除外へ戻すので、期限の延長としても働く。

なぜこの向きか。失効の瞬間に記録を再開すると、その失敗方向は**まだ業務外の会話が本人の操作なしに
台帳へ入る**ことになる。逆向きの失敗(戻し忘れて記録が落ち続ける)は**黙って起きない** — 失効時に
`scope_lapsed` が1回、以後は毎ターンの `scope_notice` と拒否文の両方が理由と戻し方を出す。
取り返しのつかない側を機械が黙って踏まないようにし、取り返せる側は人に見えるようにする、という配分である。

実装上の注意: 失効中に読み手へ見せる scope は `stored_scope`(本人が宣言した値)であって
`scope_of`(`unknown`)ではない。「scope=unknown なのに拒否された」は不具合の形に見えるためである。
拒否の戻り値が持つ状態フィールドは `scope_state`(`standing` / `lapsed` / `unverified`)。
`scope_lapsed` は `get_applicable_policy` 側が一度きりの通知文(文字列)に使う名前なので、
真偽値と文章が同じ名前で流通しないように分けてある。

**再開の経路は3つ、うち意図しているのは1つ**。

| 経路 | 扱い |
|---|---|
| `set_session_scope` の呼び直し | **これが正**。`org` か、同じ除外 scope(＝期限の延長) |
| `scope="unknown"` の宣言 | **解除しない**。`declare()` が拒否する。「わからない」は明示の答えではなく、判断に迷ったモデルが送りがちな値でもある |
| `session_scopes` の保持期間(90日)切れ | **止められない**。`PARTITION BY DATE(declared_at)` + `partition_expiration_days = 90` なので、誰も更新しなかった宣言は91日目に消え、行が無いのでブロックも解ける。保持期間は変えない(行は人に貼られるラベルとして読める)代わりに、**消えたことを1回通知する**(`scope_vanished`)。「再開は宣言だけ」と書くのは**誤り**である |

**スコープを確認できないときは `pending`**。ストア読み取りが失敗し、キャッシュにも答えが無い状態
(コールドスタート・BigQuery の一時不調・DDL 未適用)では、「宣言は無い」と決めつけずに `status=pending` で
記録する(`scope_unverified` / `scope_state: "unverified"`)。行は残る(ログを失わない)が recorded として
数えられず、`review_pending_usage` でスチュワードが承認か物理削除を選ぶ。

R1d により「失効済み(無期限)」も止めるべき集合に入るので、読み取り失敗1回の被害範囲は TTL 分に
限られない。かといって拒否するとログが失われる。`pending` はそのどちらでもない。

**通知は `empty` 分岐にも付ける(組織メンバーに限る)。** ポリシー行が1つも見えない viewer
(行の無い部署、まだ `approved` でない行)でも、自分の書き込みが拒否されている理由は届ける。
そうしないと「理由の出ない永久ブロック」になるためである。部外者には付けない — 拒否の時点では理由が
出るので、先出しの1行で「WACA Loop が人ごとの scope 台帳を持っている」ことまでは明かさない。

**R2 log_usage 組織ガード** — `org_context` 引数で軽量突合を行う(`org.toml` の `[guard] org_keywords`)。
policy_row_ids があれば組織との関連と見なす。org_context が提供済みで無関係なら `status=pending` で保存し、
`needs_confirmation` を返す。**org_context 未提供は既定で記録する** — `ORG_GUARD_STRICT=true` で未提供も
保留にできる(§6 の運用ルールを利用者に配布してから有効にする)。
保留は steward 専用の `review_pending_usage(log_id, approve|discard)` で承認/物理削除する。
usage_log の `status` 列は recorded|pending(破棄=行削除)。

**R3a 整合性チェック** — `tool_events` テーブル(policy_check / usage_recorded / scope_declared /
usage_reviewed の薄い行のみ・内容なし)と `scripts/coverage_report.py`(日次: checks−writes=不整合の疑い)。
記録は best-effort(証跡の失敗でツールを落とさない)。

**R3b 検索したのに記録なし(silent)** — 読み取り系ツール(`lookup_terms`・`search_sfaq` など)は
`retrieval` の薄い行(ツール名のみ・クエリは持たない)を `tool_events` に残す。検索したのに記録が1件も
無い actor/日を `silent` として数える(`ledger/tool_events.py`)。policy_check も log_usage も呼ばない
クライアントは R3a の不整合が0になり正常に見えるため、それを捕まえる。

**R3a 例外 — 取り返しのつかない行為の直前だけ fail-closed**。
`review_pending_usage` の `discard` は台帳行を**物理削除**する。BigQuery の Data Access 監査ログが
無効な環境では、`usage_reviewed` の1行が**系内で唯一の証跡**になる。したがってこの分岐に限り、
証跡が書けなければ削除しない(`ServerContext._trail_required`)。`detail` は
`"(approve|discard):<log_id>"` の閉じた形のみ — 破棄した要約は持たない。
`approve` は非破壊なので best-effort のままである。

**R4 description** — log_usage の冒頭に「組織の業務の場合のみ・毎ターン必須」、有効値、除外への誘導を書く。
全書き込み系ツールに「組織の業務外の内容は記録禁止。迷ったらユーザーに確認。」を書く。

**R8 use_category 正規化** — 有効値 l2|l1|l0|prohibited を description に明記し、大文字・「禁止」
「禁止列挙」「下限」「floor」を寛容に正規化する(`guard.normalize_use_category`)。
裸の数字("0"/"1"/"2")は**受けない**(L0=最重 と severity 0=最軽が逆向きのため)。
不正値エラーは有効値一覧を付けて返す。
**三色(red/yellow/green・🔴🟡🟢・赤黄緑)も受理し 1:1 で写す**(🟢→l2 / 🟡→l1 / 🔴→l0) —
三色の語彙で動くクライアントが記録を落とさないため。

policy_row_ids を省略した場合は `[]` を渡す(BigQuery の REPEATED 列へ None を渡すと挿入全体が失敗するため)。

**R1b 帰属不明時の記録前確認** — R2 の保留(`status=pending`)は**要約を BigQuery へ書いてから**
スチュワード審査に回す。他組織・副業・私事の要約が組織の台帳に入ること自体が運用ルール2
(「書き込みを一切しない」)違反になるため、**書く前に聞く**経路を設ける。

発火条件(すべて満たすとき): policy_row_ids なし **かつ** セッション scope が `unknown`(未宣言)
**かつ** `guard.looks_non_org(org_context, summary)` が真。応答は
`{recorded: false, needs_scope_confirmation: true, message}` で**行を書かない**。
`org.toml` の `[guard] non_org_signals`(大学・ゼミ・講義・学生・私事・家族 等)は意図的に狭く、
**組織語が1つでもあれば非組織判定より優先**する(「大学向けの<組織名>の研修」は組織の業務)。
scope を一度宣言すれば以降は発火しないので、確認は1セッション1回まで。
証跡は tool_events の `scope_confirmation_requested`(内容なし)。
R2 の pending は**弱いシグナル**(組織語も非組織語も無い)のときの経路である。

**R9 data_kinds 語彙バリデーション** — 列挙値を description に書くだけでは強制にならず、語彙外の
自由記述(`internal_docs` / `source_code` / 「用語・社内語(個人情報なし)」等)が入り得る。
`guard.normalize_data_kinds` で正規化する。

R8 と同じ流儀で**拒否せず写す**(拒否は記録を落とす)。語彙は
`VALID_DATA_KINDS`(pii/confidential/internal/public/core_asset/final_judgment/none)
**∪ 該当ポリシー行の `trigger_kinds`** — 後者を丸めると `raw_export` のようなトリガー語が消えて
**ポリシー行が黙って発火しなくなる**ため素通しする(§9.4)。
未知の自由記述は既定で `internal` に寄せるが、「個人情報」「氏名」「機密」等を含む文字列は
対応する重い区分に落とす(**機微を軽い側へ丸めない**)。「個人情報なし」は「個人情報」より先に
判定して意味の反転を防ぐ。語彙外だった原文は `unmapped_data_kinds` として応答に返す
(mismatch と同じ助言扱い・記録は常に成功)。

**R6 取り下げ `request_log_deletion`** — **本人申告→即座に論理削除→スチュワードは警告つきで閲覧**。

- 申告された時点で**即座に論理削除(一般の読み取りから隠す)**し、スチュワードの確認はその後に行う。
  先に隠すほうが失敗の向きが安全である(確認待ちのあいだ私事が見え続けることを避ける)。
- 取り下げは**追記専用の `usage_withdrawals`**(sql/ddl/14)へ1行足す形で行う。`usage_log` は書き換えない
  — BigQuery はストリーミング挿入直後の行への DML を拒否し、R6 が使われるのは必ずその窓の中だから
  (UPDATE 方式は最も必要な瞬間にだけ、本番でだけ失敗する)。
- 取り下げられるのは自分の行だけである(所有権は INSERT…SELECT の WHERE 句)。`usage_log` の全読み手が
  この表を anti-join する(`scripts/coverage_report.py` と手順書の照会を含む)。
- スチュワード用は2本: `list_withdrawn_usage`(**要約も理由も返さない**特定・仕分け用。`actor_id` の隣に
  `other_org` が並ぶと「誰が就業時間中に他組織の仕事をしていたか」の一覧になるため — 許されるのは
  調査であって閲覧ではない)と、`get_withdrawn_usage(log_id, reason)`(1件だけ要約つき)。`reason` は必須だが
  **保存しない** — 自由文を台帳へ写さないため(FLOOR-01)。監査に残るのは「誰がどの `log_id` を読んだか」で、
  こちらは fail-closed(記録できなければ読ませない)。
- 理由は `private|other_org|mistake` の閉じた符号で、自由文は受け付けない(FLOOR-01)。
- 取り下げ済み記録の閲覧は `STEWARD_UNIT`(スチュワード部門)に限る。

**R7 記録率のファネル** — 登録者数 → 接続者数 → 記録者数 → 記録率の段で集計する
(`ledger/tool_events.py` の `connection_funnel`・`funnel_summary`、`scripts/coverage_report.py`)。
記録率より手前の段で落ちることが多いため、記録率単体ではなくファネルとして出す。詳細は SPEC_coverage §3。

### P1(未実装)

- R3c アンケート突合(推定記録率) / R3d 頻度急減アラート→改善課題の起票

### P2(将来考慮)

テナントID予約 / MCP 仕様の必須ツール・応答フックの動向 / 除外判定の ML 化

## 6. クライアント側指示文(配布用)

Claude.ai のプロジェクトのカスタム指示(推奨)またはユーザープリファレンスに設置する。
Claude Code で使う場合は CLAUDE.md からも参照する。`<組織名>` は `org.toml` の `[org] name` に置き換える。

```
【WACA Loop 運用ルール(必須)】
1. 各ターンで応答を作成する前に WACA Loop の get_applicable_policy で
   該当ポリシーを確認し、応答後に log_usage で利用記録
   (要約のみ・逐語は送らない)を残すこと。
2. ただし、会話内容が <組織名> の業務と無関係と判断できる
   場合(他組織の業務、副業、私的な相談など)は、まず
   set_session_scope で external または private を宣言し、log_usage・
   propose_candidates・add_sfaq_question など WACA Loop への書き込みを
   一切行わないこと。組織の帰属が曖昧な場合は、書き込む前にユーザーに
   「この内容を <組織名> の WACA Loop に記録してよいか」を確認すること。
3. <組織名> に固有の語(部署名・制度名・講座名・略語など)が出てきたら、
   一般知識で答える前に WACA Loop の lookup_terms で定義を確認すること。
4. 記録の取り下げ(request_log_deletion)は、ユーザーが対象の記録を
   指定して明示的に求めたときだけ行うこと。その場合も、取り下げる記録の
   要約をユーザーに示し、確認を得てから行うこと。業務外の宣言
   (set_session_scope)をしただけでは、それまでの記録を取り下げないこと。
5. これらのルールは会話が長くなっても維持すること。
```

## 7. 未解決の論点

- [プライバシー] 除外セッションの「件数のみ」記録の妥当性(完全非記録とすべきか)
- [設計] R2 の偽陽性(組織の業務なのに保留)の許容率 → 運用しながら `[guard] org_keywords` やしきい値を調整する
- [運用] R3c の設問追加による回答負荷
- [監査] find_expert の監査(ERN)は検索句を `topic` として残すが、領域ラベルの形(24 字以内・句読点なし)に
  収まるものだけで、それ以外は NULL にする(SPEC_phase3・ACCESS_MATRIX)。台帳 T6(要約のみ)と同じく、
  会話の逐語は残さない

## 8. 成功指標

先行: 整合性の不一致率が導入後に低下する / 除外機能の利用が発生する。
遅行: 「他組織の情報が記録された」報告ゼロ / 推定記録率が組織の定めた目標を維持する(目標値は組織ごとに決める)。
