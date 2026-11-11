# 開発者向けガイド

開発者向けに、テストの実行(クラウド不要)と、自分の PC で起動した **ローカル** のサーバーに
AI クライアントを接続する手順を示す。
サーバーは各自の PC 上で **stdio** で動作し、**共有の** BigQuery を参照する。利用者の識別には Google
OAuth を使う(SPEC_phase1 §5)。日常の利用者は、公開サーバー(Cloud Run)に接続する
([MANUAL_users.md](MANUAL_users.md) を参照)。本書は開発者向けの経路である。

```
各自の Claude Desktop / Claude Code ──(stdio)──► waca-loop(各自の PC・各自の OAuth ログイン)
                                                      │
                                                      ▼
                                      共有の BigQuery(用語・台帳・フィードバック)
```

## 0. テストの実行(クラウド不要)

コードを変更・確認するための手順で、組織での運用には使わない(運用は [SETUP_gcp.md](SETUP_gcp.md) を参照)。

Python 3.11 以上が必要。リポジトリは **ZIP ではなく `git clone` で取得すること**。GitHub の Download ZIP で
取得すると git のリポジトリにならず、hook がリポジトリの場所を `git rev-parse` で調べられないため、
`tests/test_hooks.py` のテストが1件失敗する。

```bash
git clone https://github.com/wacasg/waca-loop.git
cd waca-loop
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m pytest -q
```

macOS には `python` コマンドがないため、仮想環境は `python3 -m venv` で作る(有効化した後は `python` で動く)。
テストはすべてオフラインで実行できる(BigQuery・Google アカウントは不要)。最後に `passed` と表示され
`failed` がなければよい(`skipped` は未実装の雛形のテスト)。実行時に MCP SDK の `validate_token_resource` が
非推奨(deprecated)であるという警告が表示されることがあるが、テストの成否には影響しないため無視してよい。
架空の組織「サンプル協会」の設定と名簿を使い、登録内容を確認することもできる。

```bash
ORG_CONFIG=sample/org.toml python scripts/register_members.py sample/members.json
```

## A. 管理者による初回準備(Google Cloud の認証情報が必要)

1. [SETUP_gcp.md](SETUP_gcp.md) に従って Google Cloud・BigQuery・OAuth を構築する。API の有効化、
   Vertex AI への接続と埋め込みモデルの作成、`scripts/init_bigquery.py` の実行、**デスクトップ アプリ用の
   OAuth クライアント**の作成を行う。
2. 組織設定を作成する。`org.example.toml` を `org.toml` にコピーし、`CHANGE-ME` をすべて置き換える
   (試すだけなら `ORG_CONFIG=sample/org.toml` を指定する)。
3. 利用者を登録し、メールアドレスで事前登録する(persons と `oauth_identity_map`)。
   `members.example.json` をコピーして `python scripts/register_members.py members.json` を実行するか、
   組織シートから登録する([MANUAL_users.md §5.1](MANUAL_users.md#51-利用者の登録) を参照)。
   アクセス制御は `unit_path` に基づく(SPEC_phase1 §3.3)。たとえば `steward` ユニットの利用者は、
   そのユニットが対象の機微な項目やインシデント関連の項目を閲覧できる。組織全体の項目は、全員が持つ
   組織ユニット(`[units] org`)で共有される。
4. 接続に必要な値を開発者に共有する。これらは利用者ごとの秘密情報ではない(ログインは利用者ごとに行う)。
   `GCP_PROJECT`、`DS_*`、`BQ_LOCATION`、`EMBEDDING_*`、デスクトップ用の `OAUTH_CLIENT_ID` /
   `OAUTH_CLIENT_SECRET`、および `org.toml`。

## B. 各開発者の準備

1. リポジトリを取得してインストールする。`make setup` は仮想環境の作成と `pip install -e '.[dev]'` を行う。
   ```bash
   git clone <リポジトリのURL>
   cd waca-loop
   make setup
   cp .env.example .env
   ```
   `.env` には管理者から共有された値を記入し、`org.toml` をリポジトリの直下に置く。
2. AI クライアントに MCP サーバーを登録する。

   **Claude Code**
   ```bash
   claude mcp add waca-loop -- waca-loop
   claude mcp list
   ```

   **Claude Desktop** — `claude_desktop_config.json` に次を追加する。
   ```json
   {
     "mcpServers": {
       "waca-loop": {
         "command": "waca-loop",
         "cwd": "/absolute/path/to/waca-loop"
       }
     }
   }
   ```
   `waca-loop` は `pip install -e .` で導入されるコマンドで、`cwd` の `.env` を読み込む。

3. 初回の起動時にブラウザで Google の認証が開く。管理者が事前登録したアカウントでログインすると、
   Google の `sub` が自動で利用者に紐づけられる。トークンは `OAUTH_TOKEN_STORE`(`.tokens/`。git の管理
   対象外)に保存される。

## C. 動作確認

クライアントで WACA Loop のツールが使用できることを確認する。

- 投入済みの用語を `lookup_terms` で検索すると、定義と公開範囲が返る。
- 任意の質問をすると、システムプロンプトに従ってクライアントが `log_usage` を呼び、台帳に行が追加される。
- `get_pending_survey` を呼ぶと、月次アンケートが返る(回答は任意)。
- `list_improvement_tasks` を呼ぶと、見てよいタスクだけが返る。`steward` ユニットの利用者にはすべてのタスクが、
  それ以外の利用者には「担当ユニットが自分の所属ユニットであるタスク」と「担当ユニットの無い、インシデント
  以外のタスク」が表示される。インシデント由来のタスクは、担当ユニットの利用者と steward だけが見られる
  (`src/waca_loop/improvement/acl.py`)。

## リポジトリ外で必要な作業

- Google Cloud の構築と OAuth クライアントの作成(認証情報を扱うため管理者が行う)。
- `.env` の値と `org.toml` の開発者への配布。
- claude.ai などからリモートで利用する場合の Cloud Run へのデプロイ([SETUP_gcp.md §8](SETUP_gcp.md))。
