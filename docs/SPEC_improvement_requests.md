# SPEC — WACA Loop の不具合・改善要望を受けて直しきる仕組み

正典。`docs/SPEC_phase1.md` §X の規約に従い、コードは本書を `§X` で参照する。

## 0. 何を解こうとしているか

WACA Loop を使っていて「ここが使いにくい」「これが動かない」と気づいても、仕組みが無ければその気づきは
**どこにも残らず**、残っても直したことが起案者に返らない。この仕組みは次の一周を閉じる:

```
気づく → 記録 → 管理者のセッション開始時に最優先で提示 → 設計・開発・テスト → 状態を書き戻す
                    ↑                                              ↓
              WACA Loopを使っていない人はフォームから ────────────── 起案者がシートで見る
```

## 1. 基本方針

| # | 論点 | 方針 |
|---|---|---|
| 1 | 記録の「正」 | **WACA Loop（BigQuery）が正**。スプレッドシートは書き出し先（人が読む鏡） |
| 2 | Issue 起票 | **AI が下書き → 管理者が押す**。自動起票はしない（CLAUDE.md の承認境界を崩さない） |
| 3 | 「最優先で表示」の意味 | **管理者が Claude で WACA Loop を開発するときのセッション開始時に、要望が上がったことを提示する** |
| 4 | フォームの入口 | **誰でも（ログイン不要）** |
| 5 | フォームの防御 | 防御は置かず、**同時アクセスが大量にあったときのみアラート**する |
| 6 | 起案者への報告 | **シートの状態列を更新するだけ**。AI はメールを送らない |
| 7 | 起案者名 | **お名前欄は任意。書かなければ匿名**（匿名可否の設問は置かない） |
| 8 | 「見送り」を決められる人 | **WACA Loop の管理者（`owner` ユニット）だけ**。スチュワード全員ではない |
| 9 | 種別の「その他」 | **残す** |

## 2. データモデル

新テーブル `<DS_FEEDBACK>.improvement_requests`（DDL: `sql/ddl/12_improvement_requests.sql`）。

既存の `improvement_tasks` とは**別立て**にする。あちらは申告・インシデント由来で
スチュワードが捌く導線、こちらは「WACA Loop という製品への要望」で開発者が捌く導線。混ぜると
どちらの一覧も使い物にならなくなる。ACL とストアペアの作法は既存に完全に揃える。

```
request_id     STRING NOT NULL
kind           STRING NOT NULL   -- bug | improvement | other
title          STRING NOT NULL
body           STRING NOT NULL   -- 症状・期待・再現手順。逐語ログは入れない（INV-1）
reporter_id    STRING            -- person_id。匿名希望なら NULL（§1-7）
reporter_label STRING            -- 表示名 or "匿名"。報告先の解決に使う
channel        STRING NOT NULL   -- mcp | form
status         STRING NOT NULL   -- unverified | open | designing | implemented | released | wontfix
owner_unit     STRING            -- resolve_owner_unit 経由。推測しない
scope_tier     STRING NOT NULL   -- 3軸。ほかのレコード種別と同じ列名にして、判定は
scope_audience STRING NOT NULL   -- retrieval/acl.py の visible_to / acl_where_clause に
scope_sensitivity STRING NOT NULL -- そのまま任せる（要望専用の ACL 方言を作らない）
issue_url      STRING            -- 管理者が押したあとに埋まる
resolution     STRING            -- 実装内容の要約。シートへ書き戻す本文
created_at     TIMESTAMP NOT NULL
updated_at     TIMESTAMP NOT NULL
```

- `kind` は **`bug` / `improvement` / `other`** の3値（§1-9）。
  `other` は捨てずに受け、§4 の提示にも並べる。**取り込みで機械的にどちらかへ寄せない** —
  寄せると「不具合の件数」を数えたときに混ざっているのに気づけなくなる。仕分けは
  `unverified → open` に上げる人がする（§3.2）。
- `severity` / `priority` の**数値列は置かない**。「最優先で提示」は §4 の並べ方で表現する。
- `body` に逐語プロンプトを入れない（INV-1・T6 と同型）。フォーム経由の本文は
  `pii.scrub_text` を通してから格納する。

## 3. 入口は2つ

### 3.1 MCP ツール `report_improvement_request`（WACA Loop を使っている最中）

`ServerContext.report_improvement_request(kind, title, body, anonymous=False)`。
`server.py` の作法どおり、ロジックは `ServerContext` のメソッド、`build_mcp()` は薄い束ね。

- **`_require_identity` 必須** — 未解決 viewer からの共有層書き込みは落とす。
- `owner_unit` は `retrieval/acl.resolve_owner_unit(viewer)` で決める。`next(iter(viewer.units))`
  は使わない（複数所属なら呼び出し側が名指しする / 名指しが無ければ拒否）。
- `anonymous=True` なら `reporter_id=NULL` / `reporter_label="匿名"`。
  **匿名は事後に取り消せない**ので、書き込み時に一度だけ確認する（スキル側の作法）。
- **匿名は person_id を落とすだけでは成立しない**。復元経路が2本ある:
  ① `owner_unit` に起案者の所属を刻むと、数名の単位から出た匿名要望は所属だけで割れる
  ② `_trail` が actor_id 付きで時刻を刻むので、同時刻のイベントと突き合わせれば秒精度で
  一意に決まる。したがって匿名のときは **`owner_unit` を持たせず `org` 階層で受け、
  台帳にも痕跡を残さない**。代償はその要望が全社可視になること — 狭く見せて割れるより良い。
- `channel="mcp"` / `status="open"`（WACA Loop 経由は本人性が OAuth で担保済み）。

### 3.2 Google フォーム（WACA Loop を使っていない・そもそも動かない）

ログイン不要（§1-4）。したがって**本人性が無い**。ここが設計上いちばん効く分岐:

- フォーム経由は `channel="form"` / **`status="unverified"`** で入る。
- **`unverified` は §4 のセッション提示に出さない。** 事務局が目で通して `open` に上げたものだけ
  管理者の作業開始時に並ぶ。ログイン不要の入口を、開発者の作業キューに直結させない。
- 取り込み経路: フォーム → 回答シート → `scripts/ingest_improvement_form.py`（定期実行
  から呼ぶ）。フォームから WACA Loop へ直接書く経路は作らない（外向きの口を増やさない）。
- **同時アクセスのアラート（§1-5）**: 取り込み時に「直近1時間の form 由来の行数」が閾値
  （既定 30）を超えたら `tool_events` に1行残し、取り込みを止めて管理者に知らせる。
  行を捨てない・全件通しもしない。閾値は config 値にする（リテラル禁止）。

## 4. 「最優先で提示」— スキル `improvement-requests`

`.claude/skills/improvement-requests/SKILL.md`。管理者が **waca-loop リポジトリで Claude Code を
開いたとき**に発火する（§1-3）。

1. `list_improvement_requests(status="open")` を呼ぶ。
2. **未対応の要望があれば、その回の最初の応答の冒頭に出す。** 出す情報は
   件名・起案者ラベル・上がった日だけ。本文は管理者が選んだものだけ展開する。
3. 管理者が1件選んだら、そのセッションで **設計 → 実装 → テスト** まで走る。
   - 設計時に `status="designing"` へ。
   - 実装後は `pytest` 全緑・`ruff` クリーン・`spec-guardian` と `acl-privacy-auditor`
     （src/ か sql/ を触ったなら必須・ブロック権あり）。
4. 完了時に `resolution` を書き `status="implemented"`。デプロイまで行ったら `released`。
5. **Issue は下書きまで**（§1-2）。タイトル・再現手順・影響範囲を生成して `gh issue create`
   のコマンドを提示し、押すのは管理者。押されたら `issue_url` を埋める。
6. **報告はシートの状態列の更新のみ**（§1-6）。メールも Slack も送らない。

「最優先」は AI が計算した優先度ではなく、**未対応の要望を他の話題より先に出す**という
提示順のこと。`priority` 列を作らない（§2）のはこのため。

## 5. シートへの書き出し

実体は `src/waca_loop/improvement/sheet_mirror.py`（本番のサーバが §10 の即時反映で
同じものを使うのでパッケージ側に置く）。`scripts/export_requests_to_sheet.py` はその
夜間実行の入口。WACA Loop → シートの**一方向**。

- シートが正になる経路を作らない（§1-1）。シート側で status を書き換えても WACA Loop は読まない。
- 書き出す列: 件名 / 種別 / 起案者 / 状態 / 実装内容 / 上がった日 / 更新日。
- **出口で可視域を捨てない**。ACL を通した結果をそのまま全社シートへ
  平坦化すると、WACA Loop では見られない要望の**件名と実装内容**がシート経由で読める。件名は本文と
  同じ種類の情報を運ぶ（「A部の◯◯取り込みが失敗する」）。したがって書き出すのは:
  - `body` 全文を出さない
  - **`owner_unit` の付いた部内スコープの行を出さない**（org 可視の行だけ）
  - **`unverified`（仕分け前）を出さない** — 社外→全社シートの直通路になる
- 起案者の表示は**検証済みかどうかを残す**: WACA Loop 経由=「記名（WACA Loop経由）」／
  フォームの名前欄=「（自己申告）◯◯」／匿名=「匿名」。同じ見た目にすると
  「匿名」も「記名」も意味を失う。

## 6. 受け入れ基準（R1〜R12・オフライン）

| R# | 観点 |
|---|---|
| R1 | 未解決 viewer からの `report_improvement_request` が拒否される（`_require_identity`） |
| R2 | `owner_unit` は `resolve_owner_unit` 経由。複数所属で名指しが無ければ拒否 |
| R3 | `anonymous=True` の行に `reporter_id` が残らない（モデル・行・SQL の3点検査） |
| R4 | `channel="form"` の行は `status="unverified"` で入り、`list_improvement_requests(status="open")` に出ない |
| R5 | 他部門の viewer から、部内スコープの要望が見えない（in-process と WHERE 句の二重ゲート） |
| R6 | DDL に `priority` / `severity` の数値列が無い（grep 表明） |
| R7 | 取り込みが閾値超過で止まり、`tool_events` に痕跡が残る（§3.2 アラート・閾値は `Settings.form_burst_threshold`） |
| R9 | `other` を捨てず、どちらかへ寄せもしない（§1-9） |
| R10 | 匿名行に `owner_unit` も台帳の痕跡も残らない（§3.1） |
| R11 | 書き出しが部内スコープ行と `unverified` を含まない（§5） |
| R12 | 二重ゲートが行×viewer の直積で一致する（§3.3） |

テストは `tests/test_improvement_requests.py` にあり、InMemory フィクスチャでオフライン実行する。

## 7. フォームとタブの実体

**フォームは新規 Google フォームを作る。書き出し先は組織シートの新規タブ。**
§7.1 は導入する組織が作るフォームの確定構成である。

### 7.1 Google フォームの設問

回答の受け先はフォームが自動で作るタブ（既定名 `フォームの回答 1`）。

設定: **ログイン不要**（§1-4）／回答の編集を許可しない／回答1件につき1行。

設問:

| # | 設問 | 形式 | 必須 | WACA Loop の列 |
|---|---|---|---|---|
| 1 | 報告の種類 | ラジオ（不具合 / 改善要望 / その他） | ✓ | `kind` |
| 2 | レポートのタイトル | 記述（1行） | ✓ | `title` |
| 3 | 問題の詳細や改善案 | 記述（段落） | ✓ | `body` |
| 4 | 再現手順（不具合報告の場合） | 記述（段落） | — | `body` に追記 |
| 5 | 動作環境（OS・MCPクライアント・版） | 記述（1行） | — | `body` に追記 |
| 6 | お名前 | 記述（1行） | — | `reporter_label`（空欄なら「匿名」） |

設問4・5は不具合の切り分けを速くするので採る。`body` に見出し付きで連結して格納し、
**列は増やさない**（増やすと「空欄が多い列」がシートに並ぶだけになる）。

**置いてはならない設問**:

- **「重大度を選択してください」** — §2 で `severity` / `priority` の列を置かないと決めている。
  起案者の自己申告の重大度は、列が無くても**事実上の優先度として機能してしまう**。
  「最優先」は AI が計算した優先度ではなく提示順のこと（§4）。
  切り分けの手がかりが要るなら、置く場所は起案者の自己申告ではなく、
  事務局が `unverified → open` に上げる判断のほうである。
- **「連絡先メールアドレス（任意）」** — ログイン不要のフォームで**生の連絡先を集めることになる**
  （FLOOR-01）。しかも報告は**シートの状態列を更新するだけ**（§1-6）で、こちらからメールを
  送らないと決めているので、**使い道が無い**。集める必要のない個人情報は集めない。
  代わりに設問6（お名前・任意）だけを持つ。報告先の解決にはそれで足りる。

設問3の直下に注記を置く（フォームは WACA Loop の外で、3軸ACLも scrub も効かない。ここだけが歯止め）:

> **氏名・メールアドレス・会員番号など、個人が特定できる情報は書かないでください。**
> 「◯◯さんの」ではなく「ある会員の」と書けば十分です。

**匿名可否の設問は置かない**。名前を書かなければ匿名、で足りる。
「匿名にしますか」と明示的に問うと、実名で出してよい人まで匿名を選び、実装後に報告できる
相手がいなくなる（§1-6 の報告はシートの状態列なので、匿名分は宛先が消える）。
**問わないほうが実名が残る。** 取り込み側は「お名前が空 → `reporter_id=NULL` /
`reporter_label="匿名"`」と読む。

### 7.2 書き出し先タブ

組織シートに新規タブ **`WACA Loop改善要望`** を作る（シートIDは環境変数 `IMPROVEMENT_SHEET_ID`）。
人員タブと同じファイルに置くのは、事務局が見に行く場所を増やさないため。

**フォームの回答タブ（`フォームの回答 1`）とは別物である。** あちらはフォームが自動で書く
生の受信箱で、`scripts/ingest_improvement_form.py` が読む入口。`WACA Loop改善要望` は WACA Loop が書く
出口で、状態と実装内容が返ってくる場所。**同じタブを兼ねさせない** — 兼ねると
「フォームが上書きする列」と「WACA Loop が上書きする列」が1枚に混ざり、どちらが正か決まらなくなる
（§1-1 で WACA Loop を正と決めた意味が消える）。

列（WACA Loop → シートの一方向。シート側の編集は WACA Loop に戻らない）:

| 列 | 中身 |
|---|---|
| A | 受付ID（`request_id`） |
| B | 種別（不具合 / 改善要望 / その他） |
| C | 件名 |
| D | 起案者（実名 or 「匿名」） |
| E | 状態（未確認 / 受付 / 設計中 / 実装済 / リリース済 / 見送り） |
| F | 実装内容（`resolution`） |
| G | 上がった日 |
| H | 更新日 |

**`body` 全文の列は作らない**（§5）。シートは3軸ACLが効かない場所で、部内スコープの要望本文を
全社可視へ移すことになる。本文は WACA Loop 側で `search`／セッション提示から読む。

## 8. 「見送り」の権限

`status = 'wontfix'`（見送り）を付けられるのは **owner ユニットを持つ人（WACA Loop の管理者）だけ**。他の状態遷移
（`open` / `designing` / `implemented` / `released`）はスチュワードで足りる。

**なぜ分けるか。** ほかの状態はどれも「途中経過の記録」で、間違えれば次の更新で直る。
`wontfix` だけは**「直さないと決めた」という結論**で、起案者への説明責任が伴い、
放っておくとその要望は二度と提示に出てこない（§4 は `open` しか並べない）。
記録の権限と、打ち切る権限は別物である。

**実装**: 役割ユニット **`owner`**（`org.toml` の `[units] owner`）を、WACA Loop の管理者の
`unit_path` に手で足す（`scripts/grant_role_unit.py`）。`steward` / `export_admin` と同じ扱い — **組織シートが
表現できないユニットなので、`sync_persons_from_sheet.py` の `manual_units` 経由で
再同期を生き延びる**（CLAUDE.md の carry-forward 規約）。判定は
`OWNER_UNIT not in viewer.units → PermissionError` の一行で、
**「スチュワードならできる」に寄せない**。

受け入れ基準:

| R# | 観点 |
|---|---|
| R8 | スチュワードでも `owner` でなければ `wontfix` を付けられない（他の状態遷移は通る） |

## 9. SFAQ と混ざらないか

**混ざらない。ただし混ざる場所は1箇所だけあり、そこは明示的に切る。**

### 9.1 層としては別物

| | 中身 | 誰のためか |
|---|---|---|
| SFAQ | 組織の**判断**ナレッジ（問い＋なぜそう決めたか） | 仕事をする人 |
| 改善要望 | **WACA Loop という道具への注文** | WACA Loop を直す人 |

SFAQ は「組織の仕事の判断」、改善要望は「道具の不満」。前者は組織の資産として残り続け、
後者は直ったら役目が終わる。**寿命が違うものを同じ層に置かない。**

### 9.2 混ざる唯一の場所 — 入力の窓口

利用者から見ると、どちらも「Claude に喋るだけ」である。「この検索、使いにくい」と言ったとき、
AI がそれを SFAQ の候補として拾うのか、改善要望として拾うのかが唯一の混乱点。ここは切る:

- 改善要望は **`report_improvement_request` の明示的な呼び出しでしか入らない**。
  会話からの自動抽出はしない。
- **`propose_candidates`（SFAQ 候補の自動抽出）の原料から、WACA Loop 自身への言及を外す。**
  道具への不満は組織の判断知識ではない。混ぜると SFAQ が「使いにくい」の山になる。
  実装は **落とす（skip）であって、バッチ全体の失敗ではない** — 途中で例外を投げると、
  先に書けた候補だけが残って呼び出し側は同じバッチを再送し、重複を作る。何をなぜ落としたかは
  `skipped` として戻り値に返す（判定は語の部分一致なので、誤って落としたぶんが分かるように）。

### 9.3 逆向きの橋は1本だけ架ける

要望そのものは SFAQ に入れない。ただし**実装したあと、「なぜそう直したか／なぜ見送ったか」が
判断として価値を持つことがある**（例: 「重大度欄を置かない」と決めた理由）。そのときだけ、
人が SFAQ に起こす。`raise_lesson_from_task` と同じ形で、**写しではなく人が書く**。

これで要望の一覧は要望のまま、SFAQ は判断のまま保たれる。

## 10. WACA Loop への入力をシートへ自動反映できるか

**できる。**`§5` の `scripts/export_requests_to_sheet.py` がそれである。速さの取り方だけ選ぶ:

| | 反映 | 代償 |
|---|---|---|
| **夜間バッチのみ**（§5 の設計） | 翌朝 | 起案した本人がその場で確認できない |
| **即時 + 夜間で取りこぼし回収**（推奨） | 数秒 | 実装が一段増える |
| 即時のみ | 数秒 | **Sheets API が落ちると WACA Loop への記録自体が失敗する** |

推奨は2番目。**シートへの書き込みは best-effort にし、失敗しても WACA Loop 側の記録は成功扱いにする。**
WACA Loop が正（§1-1）なのだから、鏡が一時的に曇っても本体は止めない。夜間バッチが差分を埋める。
即時のみにすると、外部サービスの都合で組織の記録が落ちる — 正と鏡の関係が逆転する。

## 11. 運用構成

**夜間の書き出し**: Cloud Run Job（コンソールエントリ `waca-loop-mirror` =
`improvement/sheet_mirror:main`）を Cloud Scheduler で **毎日1回**（例: 23:00）叩く。
即時反映が働いていれば差分ゼロだが、即時反映が失敗した分をここで埋める。
`scripts/` はコンテナに入らない（Dockerfile は `src/` だけ COPY する）ので、定期実行の
入口はパッケージ側に置いてある。夜間と即時で同じ `sheet_mirror` を使うので、出す内容が
食い違わない。

必要な環境変数: `IMPROVEMENT_SHEET_ID` / `SHEETS_IMPERSONATE_SA`。Sheets は
`cloud-platform` スコープでは通らないので、実行 SA が**自分自身を impersonate** して
spreadsheets スコープのトークンを取る（`roles/iam.serviceAccountTokenCreator` を自分に付与する）。

**導入時の段取り（コードではない）**:

- **スプレッドシートを実行SA（`waca-loop-serve@<GCPプロジェクトID>.iam.gserviceaccount.com`）に
  「編集者」で共有すること。** これが済むまで書き出しは 403 で落ちる。
- `owner` ユニットを管理者に付与する（`scripts/grant_role_unit.py`）。**このスクリプトは `owner`
  専用**にしてある — `steward`（昇格権限）と export-admin（FLOOR-02 バイパス）は、このスクリプトの
  片手間で付けてよい重さではないので意図的に外した。`--apply` は `--reason` 必須で、
  付与は `tool_events` に残る。
- `_K_CAPS["improvement_requests"] = 50`（1回の一覧の上限）を組織として承認する。
