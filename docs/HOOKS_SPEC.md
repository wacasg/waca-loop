# Claude Code hooks によるガード(G1〜G5) — 仕様

実体は Claude Code プラグイン `plugins/waca-loop`(`hooks/hooks.json` ＋ 各スクリプト)である。
目的: 「約束で守る」ルールを「機械で止まる」ルールに変える。

**実装上の要点**:

- パスは **`plugins/waca-loop/hooks/`** である(プラグインとして配布するため。リポジトリの外で作業して
  いても効く)
- G1 の `gcloud` 書込系は **block ではなく warn**。デプロイ・IAM 変更はコマンド文字列から「承認済みか」を
  判定できず、block にすると正当な作業が常に止まるため、提示を促す形にしている
- G1 は **`WACA_LOOP_ALLOW_BQ_WRITE=1` による明示承認**を受け付ける。hook は管理者の承認を観測できないので、
  「承認が出たらこの環境変数を付けて実行する」という形で承認を機械に伝える
- G1 は **BigQuery を書くリポジトリ内スクリプトの名指し許可リスト**も検知対象にする(`bq` コマンドを経由しない
  経路を止めるため。実行しているときだけ見る — ファイル名が現れただけの `grep` や `git add` は止めない)。
  呼び方は `python scripts/x.py` / `python -m scripts.x` / `python -c` の BigQuery 直叩きを見る。
  既定が dry-run のスクリプト(`register_members.py`・`grant_role_unit.py`・`purge_frozen_ern.py`)は、
  `--apply`(`--apply-freeze` などを含む)を付けたときだけ止める
- **G1 は「block」と書いてあるが、網羅的な強制ではない。** コマンド文字列から既知の書き方を止めるだけで、
  新しいスクリプト・改名・シェルラッパは抜ける。本来の強制は BigQuery 側の権限分離(書込可能なサービス
  アカウントを人と分ける)である。この hook をもって「本番は守られている」と言ってはいけない

## ガード一覧(優先順)

### G1: 本番BQ書込ガード(PreToolUse / Bash)【最優先】
- 検知: `bq` / `gcloud` / スクリプト実行のうち、書込系(load/query の DML/cp/rm 等)で、
  対象が `waca_loop_integration` データセット**以外**の本番テーブルを指すコマンド
- 動作: **block**。既存データセットへの書込は管理者の承認後に行う。新しいテーブルの試作は waca_loop_integration で行う
- 例外: `--dry-run` を含む単独のコマンド、および既定が dry-run のスクリプトを `--apply` なしで実行する
  コマンドは許可する
- 実装: `guard-bq-write.sh`(PreToolUse, matcher: Bash)。コマンド文字列の判定は保守的に行い、
  判定不能な bq 書込系は block に倒す(フェイルクローズ)

### G2: 三色語彙のハードコードゲート(Stop)
- 検知: src/ 配下の新規・変更行に 三色語彙(red/yellow/green・赤/黄/緑)や
  tier 文字列比較("personal"/"team"/"dept"/"org" の直書き比較)が入った diff
- 動作: **warn**(block にしない — 偽陽性があるため)
- 実装: git diff への grep
- **定義箇所の除外**: これらの語彙は定義元には正当に存在する。`vocab-definition-site` という語を含む
  ファイルは検査対象から外す(現在は `guard.py` がこのマーカーを持つ)。定義元のファイルを増やすときは、
  そのファイルにマーカーを付ける

### G3: DDL変更の承認リマインダ(PreToolUse / Edit・Write, matcher: sql/ddl/**)
- 検知: sql/ddl 配下の編集
- 動作: **warn**「DDL・移行クエリは提示し、管理者の承認後に実行する」
- 補足: 編集自体は止めない。適用(bq 実行)は G1 が止める

### G4: レビューゲート未実施の検知(Stop)
- 検知: src/ または sql/ に差分があるのに、セッション中に spec-guardian /
  acl-privacy-auditor の実行痕跡が無い状態でターンを終えようとした
- 動作: **warn**(レビューのリマインド)
- 実装メモ: 実行痕跡はエージェント実行時に review-stamp を touch する運用で近似する(厳密な検知はしない)

### G5: 個票生テキスト流出ガード(PreToolUse / 外部送信系ツール)
- 検知: Drive/Slack/Gmail 等へ送る内容に PII パターン(メール・電話・会員番号・〒)が含まれる
- 動作: **warn**(FLOOR-01。get_pii_anonymization_guidance は列名のみ、の原則を提示)
- 実装: src/waca_loop/ingest/anonymize.py の**公開シンボル `detectors()`/`MASKS`/`scrub_text`** を import して
  再利用する(正規表現の再宣言禁止 — 二重管理によるずれを防ぐ)

## warn の届け方 — `notify()`

Claude Code の hook では、exit 0 の**素の stdout** がモデルのコンテキストへ入るのは
`UserPromptSubmit` / `UserPromptExpansion` / `SessionStart` の**3イベントだけ**である
(公式ドキュメント `code.claude.com/docs/en/hooks`)。それ以外ではデバッグログに書かれるだけで、
トランスクリプトにも出ない。`PreToolUse` と `Stop` も例外ではない。

そのため warn のゲート(G2〜G5)は、`_lib.sh` の `notify()` で JSON を返す:

```json
{"systemMessage": "..."}
```

`systemMessage` は**ユーザーに表示され、かつモデルのコンテキストへ system message として入る**。exit 2
(ブロック)と違い会話を止めないので、**偽陽性のある warn にはこれが正しい** — Stop で exit 2 を使うと
「止めさせない」になり、無限ループの危険がある。G1 の block は exit 2・stderr で返す。

**新しいゲートを足すときは `notify()` を使うこと。素の `echo` は書かない。**

`log_usage` の未実行を Stop フックで検知するゲートは設けない(上の理由で Stop の出力がモデルに届かず、
サーバー側の nudge で足りるため。`docs/SPEC_coverage.md` C3・C4)。

## 非目標

- 人間の判定事項(FLOOR-03 と同型)の自動化はしない
- ネットワーク遮断・権限剥奪などプラットフォーム側の制御は扱わない
