# sample/ — 記入例(架空の組織「サンプル協会」)

WACA Loop を導入する際に作成する設定ファイルと名簿の記入例である。
人物・メールアドレス・部署はすべて架空のものである。

| ファイル | 内容 | 導入時の作成方法 |
|---|---|---|
| `org.toml` | 組織設定(組織名・ドメイン・特別なユニット・業務判定の語・部署名の対応表) | リポジトリ直下の `org.example.toml` を `org.toml` にコピーして記入する |
| `members.json` | 名簿(氏名・メールアドレス・所属ユニット・役割) | リポジトリ直下の `members.example.json` を `members.json` にコピーして記入する |
| `terms.json` | 用語(5語) | 動作確認用。`python scripts/seed_terms.py sample/terms.json` で投入する |
| `policy.json` | AI ポリシー(禁止事項2行・業務ルール3行) | 動作確認用。`python scripts/seed_policy.py sample/policy.json` で投入する |

`org.toml` と `members.json` は組織の実データとなるため、git の管理対象外としている
(`.gitignore` で除外済み)。リポジトリにはコミットしないこと。

## 動作確認(クラウド環境なし)

名簿の登録内容を確認する(BigQuery への書き込みは行わない)。[docs/CONNECT.md](../docs/CONNECT.md) の
「0. テストの実行」の手順で仮想環境を作り、有効にした状態で実行する(macOS では仮想環境の外に `python` コマンドがない)。

```bash
ORG_CONFIG=sample/org.toml python scripts/register_members.py sample/members.json
```

テンプレートのままでは、修正箇所を表示して停止することを確認する。

```bash
python scripts/register_members.py members.example.json
```

実際に登録する(`--apply`)には、Google Cloud の構築([docs/SETUP_gcp.md](../docs/SETUP_gcp.md))が
完了し、`org.toml` に `CHANGE-ME` が残っていないことが必要である。

## 導入手順

1. `org.example.toml` を `org.toml` にコピーし、`CHANGE-ME` をすべて置き換える
   - `[org] name` … 組織名
   - `[org] workspace_domains` … Google Workspace のドメイン(使用していない場合は `[]`)
   - `[guard] org_keywords` … 組織の業務であることを示す語(組織名・製品名・部署名など)
   - `[unit_map]` … 組織シートで人員を管理する場合のみ記入する(シート上の部署名 → ユニットID)
   - `[pii] id_column_names` … 任意。組織固有の会員番号などの列名
2. `members.example.json` を `members.json` にコピーし、利用者を記入する
   - WACA Loop の管理者には `"roles": ["steward", "owner"]` を付与する(1名以上)
3. `python scripts/register_members.py members.json` で内容を確認し、`--apply` で登録する
4. 利用者に [docs/MANUAL_users.md](../docs/MANUAL_users.md) を配布する
   (`<サーバーURL>` と `<組織名>` を置き換えたうえで配布すること)

### 役割(roles)

| roles | 権限 | org.toml の対応項目 |
|---|---|---|
| `steward` | 共有層への昇格の承認、保留記録の審査、改善タスクの横断閲覧 | `[units] steward` |
| `owner` | WACA Loop の管理者。改善要望の「見送り」を決定できる | `[units] owner` |
| `board` | 理事会・役員会など、決定を組織全体に公開できない単位 | `[units] board` |
| `export_admin` | 理由を記録したうえで、1回の検索件数の上限を超過できる(監査ログに記録される) | `[units] export_admin` |
