# WACA Loop

WACA Loop は、組織における生成 AI の利用を管理するための MCP(Model Context Protocol)サーバーである。
利用者が普段使っている AI クライアントに接続し、組織の用語・AI 利用規程・過去の意思決定を参照可能にする
とともに、AI の利用内容を要約として記録する。返却する情報は、利用者の所属と権限に応じて制限される。

WACA Loop is an MCP (Model Context Protocol) server for governing generative-AI use in an
organization. It connects to the AI clients that members already use, gives them access to the
organization's terminology, AI usage policy and past decisions, and records AI usage as summaries.
Every response is filtered by the requesting member's unit and permissions.

Licensed under Apache-2.0. Documentation is mainly in Japanese.

---

## 主な機能

利用者は普段の AI クライアント(Claude)から利用する。ツールの呼び出しは AI が行う。

| 機能 | 概要 | 主なツール |
|---|---|---|
| 組織用語の解決 | 用語・略語・部署名・人物を、表記ゆれを含めて検索する(キーワード検索とベクトル検索の併用) | `lookup_terms` |
| AI ポリシーの配信 | 作業内容に該当する組織の AI 利用規程を返す(採用レベル 0〜2、禁止事項と代替手段) | `get_applicable_policy` |
| AI 利用の記録 | AI の利用内容を要約のみ台帳に記録する(原文は保存しない)。組織外・私的な会話は記録対象外 | `log_usage` / `set_session_scope` |
| 自分の記録の書き出し | 利用者が自分の台帳記録だけを一覧と CSV で書き出す。他人の記録は出せない | `export_my_usage` |
| 権限に応じた情報提供 | 所属部署・公開範囲・機微度の3軸で、閲覧者ごとに返す情報を制限する(DB とアプリケーションの二重チェック) | 検索系ツール全般 |
| 判断知識の蓄積 | 意思決定とその理由を「問い＋判断」(SFAQ)の形で蓄積し、検索できるようにする | `search_sfaq` / `propose_candidates` |
| 改善要望の管理 | WACA Loop 自体への不具合報告・改善要望を受け付け、対応結果を起案者に返す | `report_improvement_request` |

### 自分の記録を書き出す(`export_my_usage`)

利用者は、AI クライアントに「自分のログを出力して」と頼むだけで、自分の AI 利用記録を
一覧と CSV(ヘッダ行つき)で受け取れる。

- 書き出せるのは**呼び出した本人の記録だけ**である。誰の記録かはログイン時に確認した本人情報で
  決まり、ツールには人を指定する引数が無い。そのため他人の記録は出せない。
- 中身は台帳と同じく**要約のみ**である。会話の原文は台帳に保存していないので、書き出しにも含まれない。
- 遡る日数(既定 30 日・上限 365 日)と件数(既定 200 件・上限あり)を指定できる。
- 取り下げた記録は `withdrawn=true` として残し、要約は表示しない。
- 本人を確認できない場合や、組織の利用者として登録されていない場合は、理由を添えて空の結果を返す。
- 返すのは、**いまのログインを登録に結び付けた時点以降の記録だけ**である。メールアドレスが
  別の人に使い回されて同じ利用者 ID になっても、前の持ち主の記録は出ない。結び付けた時刻が
  記録されていない場合は、運営担当(スチュワード)への連絡を促す理由を添えて空の結果を返す。
  詳しくは [docs/SPEC_export_my_usage.md](docs/SPEC_export_my_usage.md)。

## 構成

```
利用者の AI クライアント(Claude)
        │  MCP(リモート: OAuth 2.0 / ローカル: stdio)
        ▼
WACA Loop サーバー(Python・Cloud Run)
        │  ├ 認証: Google ログイン(事前登録した利用者のみ)
        │  ├ 権限: 所属ユニット × 3軸 ACL
        │  └ 組織設定: org.toml(組織名・ドメイン・ユニット・業務判定の語)
        ▼
BigQuery(用語・人・台帳・ポリシー・SFAQ)
```

## 導入手順

1. **Google Cloud の構築** — BigQuery・OAuth クライアント・Cloud Run を用意する
   ([docs/SETUP_gcp.md](docs/SETUP_gcp.md))
2. **組織設定の作成** — `org.example.toml` を `org.toml` にコピーし、`CHANGE-ME` をすべて置き換える
   (記入例: [sample/org.toml](sample/org.toml))。`CHANGE-ME` が残っている間、サーバーは起動しない
3. **利用者の登録** — `members.example.json` を `members.json` にコピーして記入し、
   `python scripts/register_members.py members.json` で内容を確認してから `--apply` で登録する
   (組織シートで人員を管理している場合は、シートからの同期も可能)
4. **利用者への案内** — [docs/MANUAL_users.md](docs/MANUAL_users.md) を配布する

`org.toml` と `members.json` は組織の実データのため、git の管理対象外としている。
詳細は [sample/README.md](sample/README.md) を参照。

## ドキュメント

| 文書 | 内容 |
|---|---|
| [docs/MANUAL_users.md](docs/MANUAL_users.md) | 利用マニュアル(利用者向け・管理者向けコマンド) |
| [docs/SETUP_gcp.md](docs/SETUP_gcp.md) | Google Cloud の構築手順 |
| [docs/CONNECT.md](docs/CONNECT.md) | 開発者向け: テストの実行(クラウド不要)・ローカル(stdio)での接続 |
| [docs/SPEC_phase1.md](docs/SPEC_phase1.md) | 実装仕様の正典(中核)。コード中の `§X` は、そのモジュールが扱う領域の仕様書(`docs/SPEC_*.md`)の節を指す |
| [docs/ACCESS_MATRIX.md](docs/ACCESS_MATRIX.md) | データ種別ごとの閲覧範囲 |
| [CHANGELOG.md](CHANGELOG.md) | 変更履歴 |

## リポジトリの構成

```
src/waca_loop/   サーバー本体(server.py にツール、retrieval/ に ACL、org.py に組織設定)
sql/ddl/         BigQuery のテーブル定義
scripts/         初期構築・登録・同期のスクリプト
tests/           オフラインのテスト(tests/org.test.toml の架空組織を使用)
plugins/         Claude Code 用のガード(台帳記録の督促・本番書き込みの防止)
sample/          架空の組織「サンプル協会」の記入例
docs/            仕様・手順書
```

## 外部からの貢献

Issue・プルリクエストは受け付けていない。脆弱性の報告は [SECURITY.md](SECURITY.md) の手順で受け付ける。

## ライセンス

[Apache License 2.0](LICENSE)。Copyright 2026 一般社団法人ウェブ解析士協会 (WACA)([NOTICE](NOTICE))。
