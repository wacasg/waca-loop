# waca-loop-guard（Claude Code プラグイン）

WACA Loop の運用ルールを、**waca-loop リポジトリの外でも**効かせるためのプラグイン。

hook をリポジトリの `.claude/settings.json` だけに登録すると、他のリポジトリで組織の業務をしたときに
台帳記録の督促が効かない。プラグインにするとユーザースコープで導入でき、どのディレクトリで
作業していても同じルールが適用される。

## 入っているもの

| hook | イベント | 動作 | 根拠 |
|---|---|---|---|
| `ledger-reminder.sh` | UserPromptSubmit | 毎ターン、台帳記録の規約を再注入（**督促**） | SPEC_ops §6 |
| `guard-bq-write.sh` | PreToolUse / Bash | **既知の書き方**の本番BQ書込を block（`bq` 各種、`python scripts/<書込スクリプト>`、`python -m scripts.<同>`、`python -c` の BigQuery 直叩き）。`waca_loop_integration` と `--dry-run` は許可、gcloud 変更は警告 | G1 |
| `guard-ddl-remind.sh` | PreToolUse / Edit・Write | `sql/ddl/**` 編集時に承認手順を警告 | G3 |
| `guard-pii-egress.sh` | PreToolUse / 外部送信系MCP | 送信内容の PII を検出して警告（検出器は `ingest/anonymize.detectors()` を import・再宣言しない） | G5 / FLOOR-01 |
| `guard-stop-gates.sh` | Stop | 三色語彙・tier直書きの混入警告 ＋ レビュー(spec-guardian・acl-privacy-auditor)未実施の警告 | G2・G4 |

**hook はツール呼び出しを代行できない。** 記録したかどうかの判定はサーバ側 R3b
（retrieval あり・usage_recorded ゼロ = `silent`）が行う。督促と検出は別の層で、どちらも要る。

**G1 は網羅ではない。** コマンド文字列を見て**既知の書き方**を止めるだけで、新しい
スクリプト・改名・シェルラッパ・独自のワンライナーは抜ける。
「これがあるから本番は安全」ではなく「うっかりを止める手すり」である。本当の強制は
BigQuery 側の権限（サービスアカウントの分離）で行う。

## 導入（各マシンで1回）

```bash
/plugin marketplace add <このリポジトリをcloneした場所>/waca-loop
```

続けて `/plugin install waca-loop-guard@waca-loop`。

## 承認済みのBQ書込を通すとき

G1 は既定で block する。管理者の承認が出たら、そのコマンドにだけ環境変数を付けて実行する。

```bash
WACA_LOOP_ALLOW_BQ_WRITE=1 bq load waca_loop_ledger.usage_log ./rows.json
```

## 意図的にやらないこと

- **組織の業務かどうかの機械判定はしない。** 誤判定は「他組織の会話を組織の台帳に書く」という
  最悪の事故（SPEC_ops §1-4）に直結する。判定はモデルとユーザーの領分。
- **G2/G4 は block にしない**（偽陽性があるため警告どまり — docs/HOOKS_SPEC.md）。
