# Google Cloud 構築手順(BigQuery・OAuth・Cloud Run)

Google Cloud 上に WACA Loop のサーバーを構築する手順である。`gcloud` と `bq` コマンドで、導入先の
プロジェクトに認証済みのマシンから実行する(WACA Loop は既存のプロジェクトにデータセットを追加して
動作する。SPEC_phase1 §0)。いずれも実際のクラウドリソースを操作するため、CI などで自動実行はしない。

## 本書の読み方

- 手順は上から順に進める。全体の流れは次のとおりである。
  準備 → §0 組織設定と `.env` → §1 API の有効化 → §2 Vertex AI への接続 → §3 データセットの作成 →
  §4 用語・ポリシーの投入 → §5 OAuth 同意画面 → §6 利用者の登録 → §7 ローカルでの動作確認(任意) →
  §8 Cloud Run へのデプロイ → §9 費用と後片付け
- コードブロックの前の目印で、どこで操作するかを示す。
  - **ターミナルで実行**: Mac の「ターミナル」などに貼り付けて Enter を押す。
  - **ブラウザで操作**: Google Cloud コンソール(https://console.cloud.google.com/)で操作する。
  - **Claude のチャットに入力**: Claude の会話欄に貼り付ける(ターミナルには貼らない)。
- コマンドは**書き換えずにそのまま貼り付けられる**ように書いてある。プロジェクトIDなどの値は、
  準備4 で一度だけ入力して変数に入れ、以後のコマンドはその変数を使う。
- `read` で値を入力させるコマンドは、**1行ずつ貼り付けて**値を入力する。複数行をまとめて貼ると、
  次の行が入力値として読み込まれてしまう。
- 各手順の「確認方法」で、成功したときの表示を確かめてから次へ進む。表示が違う場合は
  「うまくいかないとき」を見る。

## 準備

### 準備1 必要なもの

- **Google Cloud のプロジェクト**(課金が有効になっているもの)と、そのプロジェクトのオーナー権限を
  持つ Google アカウント。検証だけが目的なら、専用のプロジェクトを新しく作ると後片付け(§9)が簡単である。
- **Google Cloud SDK**(`gcloud` と `bq` コマンド)。https://cloud.google.com/sdk/docs/install を参照。
- **Python 3.11 以上**と **git**。
- **OpenSSL**(署名鍵の生成に使う。macOS には標準で入っている)。

**確認方法** — ターミナルで実行:

```bash
gcloud --version
bq version
python3 --version
git --version
```

`Google Cloud SDK 〜`、`This is BigQuery CLI 〜`、`Python 3.11` 以上、`git version 〜` が表示されればよい。

### 準備2 リポジトリの取得と仮想環境

ホームフォルダの下に取得し、仮想環境(`.venv`)を作って必要なライブラリを入れる。
**ZIP ではなく `git clone` で取得すること**(Download ZIP で取得すると、hook がリポジトリの場所を
`git rev-parse` で調べられず、`tests/test_hooks.py` のテストが1件失敗する)。

**ターミナルで実行**:

```bash
cd ~
git clone https://github.com/wacasg/waca-loop.git
cd ~/waca-loop
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

macOS には `python` コマンドがないため、仮想環境は `python3 -m venv` で作る。仮想環境を有効にした後
(`source .venv/bin/activate` の後)は、`python` で動く。

**確認方法** — ターミナルで実行:

```bash
python --version
pytest -q
```

プロンプトの先頭に `(.venv)` が付き、`pytest -q` の最後に `〜 passed` と表示され `failed` がなければよい
(`skipped` は未実装の雛形のテストで、問題ない)。

### 準備3 リージョンを決める

最初に次の2つを決める。**BigQuery の場所はデータセットを作った後には変更できない**ため、ここで決めて
以後は変えない。

| 変数 | 用途 | 値の例 |
|---|---|---|
| `REGION` | Cloud Run(サーバー)を置くリージョン | `asia-northeast1`(東京) |
| `BQ_LOCATION` | BigQuery のデータセットと Vertex AI への接続を置く場所 | `US`(マルチリージョン)または `asia-northeast1` |

- `BQ_LOCATION` は、`.env`・データセット・接続・Cloud Run に渡す値の**すべてで同じ**でなければならない。
  食い違うと、サーバーがデータセットを見つけられない。本書の手順では変数 `$BQ_LOCATION` を使うので、
  準備4 で一度入力すれば自動で揃う。
- `REGION` と `BQ_LOCATION` は異なっていてもよい(例: Cloud Run は `asia-northeast1`、BigQuery は `US`。
  この組み合わせで動作を確認している)。データを国内に置く必要がある場合は、両方を `asia-northeast1` にする。

### 準備4 gcloud へのログインと変数の設定

まず gcloud にログインする。ブラウザが開くので、導入先のプロジェクトのオーナー権限を持つアカウントを選ぶ。

**ターミナルで実行**:

```bash
gcloud auth login
```

ブラウザに「You are now authenticated with the gcloud CLI!」と表示されれば完了である。

続いて、値を変数に入れる。次の3行は**1行ずつ**貼り付け、それぞれ値を入力して Enter を押す。

**ターミナルで実行**(1行ずつ):

```bash
printf 'プロジェクトID: '; read GCP_PROJECT
printf 'Cloud Run のリージョン (例 asia-northeast1): '; read REGION
printf 'BigQuery の場所 (US または asia-northeast1 など): '; read BQ_LOCATION
```

残りの変数は、入力した値から組み立てる。この部分はまとめて貼り付けてよい。

**ターミナルで実行**:

```bash
export GCP_PROJECT REGION BQ_LOCATION
export SERVICE=waca-loop-serve
export PROJECT_NUMBER="$(gcloud projects describe "$GCP_PROJECT" --format='value(projectNumber)')"
export BQ_CONNECTION="${GCP_PROJECT}.${BQ_LOCATION}.bq_vertex"
export SA="${SERVICE}@${GCP_PROJECT}.iam.gserviceaccount.com"
export MCP_PUBLIC_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
```

| 変数 | 意味 |
|---|---|
| `PROJECT_NUMBER` | プロジェクト番号(数字)。Cloud Run の URL に使われる |
| `BQ_CONNECTION` | BigQuery から Vertex AI への接続の参照名(§2 で作成する) |
| `SA` | サーバーが使うサービスアカウント(§8.2 で作成する) |
| `MCP_PUBLIC_URL` | サーバーの公開 URL。Cloud Run の URL は `https://<サービス名>-<プロジェクト番号>.<リージョン>.run.app` の形で**デプロイ前に決まる**ため、ここで組み立てておく |

作業を中断しても入力し直さずに済むよう、変数をファイル `.env.setup` に保存する(このファイルは
`.gitignore` の対象で、git には入らない。秘密の値は含まない)。

**ターミナルで実行**:

```bash
cat > .env.setup <<EOF
GCP_PROJECT=${GCP_PROJECT}
REGION=${REGION}
BQ_LOCATION=${BQ_LOCATION}
SERVICE=${SERVICE}
PROJECT_NUMBER=${PROJECT_NUMBER}
BQ_CONNECTION=${BQ_CONNECTION}
SA=${SA}
MCP_PUBLIC_URL=${MCP_PUBLIC_URL}
EOF
cat .env.setup
git check-ignore .env.setup
```

**確認方法**: `cat .env.setup` で8行が表示され、どの行も `=` の右側が空でないこと。特に
`PROJECT_NUMBER` が数字、`MCP_PUBLIC_URL` が `https://waca-loop-serve-<数字>.<リージョン>.run.app` の
形になっていること。最後に `.env.setup` と表示されれば、git の対象外になっている。

**うまくいかないとき**

- `PROJECT_NUMBER` が空で、`MCP_PUBLIC_URL` が `https://waca-loop-serve-.asia-northeast1.run.app` のように
  なる: プロジェクトIDの入力ミスか、ログインしたアカウントにそのプロジェクトの権限がない。
  `gcloud projects list` で表示される `PROJECT_ID` を確認し、3行の `read` からやり直す。
- 値を間違えた: 3行の `read` からやり直し、`.env.setup` を作り直す(上書きされる)。
- 解決しない場合は、次の文面に必要事項を貼り付けて Claude に相談する(Claude のチャットに入力)。

  ```text
  WACA Loop の docs/SETUP_gcp.md の「準備4」を実行したところ、うまくいかなかった。原因と次に実行するコマンドを教えてほしい。
  - 実行したコマンド: (ここに貼り付け)
  - 表示されたエラーの全文: (ここに貼り付け)
  - gcloud config list の結果: (ここに貼り付け)
  ```

### 準備5 導入先プロジェクトの確認

gcloud が導入先のプロジェクトを指すように設定し、課金が有効であることを確かめる。

**ターミナルで実行**:

```bash
gcloud config set project "$GCP_PROJECT"
gcloud config get-value project
gcloud billing projects describe "$GCP_PROJECT" --format='value(billingEnabled)'
```

**確認方法**: 2行目で導入先のプロジェクトIDが、3行目で `True` が表示されること。`False` の場合は、
コンソールの「お支払い」でプロジェクトに請求先アカウントを紐づける。

**ブラウザで操作**: Google Cloud コンソールで作業するときは、**毎回、画面上部のプロジェクト選択欄に
導入先のプロジェクトが表示されていること**を確認する。別のプロジェクトが選ばれたまま OAuth クライアント
などを作ると、サーバーが動かない。gcloud も再ログインの後に別のプロジェクトを指すことがあるため、
作業を再開したときは `gcloud config get-value project` で確認する(準備7)。

### 準備6 Python スクリプト用のログインと課金先の設定

`scripts/` の Python スクリプトは、gcloud とは別の認証情報(アプリケーションのデフォルト認証情報、ADC)
で BigQuery に接続する。ADC でログインし、API の利用料の課金先を導入先のプロジェクトに設定する。

**ターミナルで実行**(1行目はブラウザでのログインが終わるまで待ってから、2行目を貼る):

```bash
gcloud auth application-default login
```

```bash
gcloud auth application-default set-quota-project "$GCP_PROJECT"
gcloud auth application-default print-access-token > /dev/null && echo "ADC OK"
```

**確認方法**: `set-quota-project` の後に `Quota project "<プロジェクトID>" was added to ADC` と表示され、
最後に `ADC OK` と表示されること。

**うまくいかないとき**

- `set-quota-project` で権限エラーになる: ログインしたアカウントに、導入先のプロジェクトでの
  `serviceusage.services.use` 権限(オーナーまたは編集者に含まれる)がない。権限のあるアカウントで
  `gcloud auth application-default login` からやり直す。
- 解決しない場合は、次の文面で Claude に相談する(Claude のチャットに入力)。

  ```text
  WACA Loop の docs/SETUP_gcp.md の「準備6」を実行したところ、エラーになった。原因と次に実行するコマンドを教えてほしい。
  - 実行したコマンド: (ここに貼り付け)
  - 表示されたエラーの全文: (ここに貼り付け)
  - gcloud config list の結果: (ここに貼り付け)
  ```

### 準備7 作業を再開するとき

新しいターミナルを開くと、仮想環境と変数は消えている。作業を再開するときは、次を順に行う。
Google Workspace のアカウントでは gcloud の再認証を求められることがあるため、ログインのコマンドは
**単独で貼り付け**、ブラウザでの認証が終わってから次を貼る(まとめて貼ると、後ろのコマンドが
パスワードの入力欄に吸い込まれる)。

**ターミナルで実行**(フォルダの移動・仮想環境・変数の読み込み):

```bash
cd ~/waca-loop
source .venv/bin/activate
set -a; source .env.setup; set +a
```

**ターミナルで実行**(ログイン。単独で貼る):

```bash
gcloud auth login
```

**ターミナルで実行**(導入先の確認):

```bash
gcloud config set project "$GCP_PROJECT"
gcloud config list
```

**確認方法**: プロンプトの先頭に `(.venv)` が付き、`gcloud config list` の `account` が導入作業に使う
アカウント、`project` が導入先のプロジェクトIDであること。Python スクリプトが認証エラー
(`RefreshError`、`invalid_grant` など)になる場合は、準備6 をやり直す。

`set -a; source <ファイル>; set +a` は、ファイルに書かれた `名前=値` の行を変数として読み込む方法である。
`.env`(§0)も同じ方法で読み込める。

### 準備8 表示されても問題のない警告

- `pytest -q` の実行時に、MCP SDK の `validate_token_resource` が非推奨(deprecated)であるという警告が
  表示されることがある。テストの成否には影響しないため、無視してよい。
- §3 で `05_vector_index.sql` について `WARN: skipped … all NULLs` と表示されるのは正常である(§3 を参照)。

## 0. 組織設定(`org.toml`)と `.env`

組織ごとに異なる値(組織名、Google Workspace のドメイン、特別な意味を持つユニットID、組織の業務と
判定する語、シート上の部署名とユニットIDの対応)は、`.env` やコードではなく `org.toml` に記載する。

**ターミナルで実行**:

```bash
cp org.example.toml org.toml
```

`org.toml` は git の管理対象外である。テキストエディタで開き、`CHANGE-ME` をすべて置き換えること
(架空の組織の記入例は `sample/org.toml`。動作確認だけが目的なら `cp sample/org.toml org.toml` で
記入例をそのまま使ってもよい)。`org.toml` が存在しない場合や `CHANGE-ME` が残っている場合、サーバーと
登録スクリプトは起動を止め、修正箇所を表示する。別の場所に置く場合は `ORG_CONFIG` にファイルの場所を指定する。

続いて `.env.example` を `.env` にコピーし、準備4 の変数の値を書き込む。

**`OAUTH_CLIENT_ID`・`OAUTH_CLIENT_SECRET` は、この時点では仮の値 `placeholder` を入れておく。**
構築用のスクリプト(`init_bigquery.py` など。`--dry-run` を含む)は、この2つが空だと起動時に止まる。
スクリプト自体は OAuth を使わないため、仮の値で動作する。本物の値は、ローカルで動作確認する場合に §5.2 で
差し替える(Cloud Run 用のクライアントは `.env` ではなく §8 で Secret Manager に保存する)。

**ターミナルで実行**:

```bash
cp .env.example .env
python - "GCP_PROJECT=${GCP_PROJECT}" "BQ_LOCATION=${BQ_LOCATION}" "BQ_CONNECTION=${BQ_CONNECTION}" "OAUTH_CLIENT_ID=placeholder" "OAUTH_CLIENT_SECRET=placeholder" <<'EOF'
import pathlib
import sys
updates = dict(arg.split("=", 1) for arg in sys.argv[1:])
path = pathlib.Path(".env")
lines = []
for line in path.read_text(encoding="utf-8").splitlines():
    key = line.split("=", 1)[0].strip()
    if key in updates:
        line = f"{key}={updates.pop(key)}"
    lines.append(line)
lines += [f"{key}={value}" for key, value in updates.items()]
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("updated .env")
EOF
```

**確認方法** — ターミナルで実行:

```bash
grep -E '^(GCP_PROJECT|BQ_LOCATION|BQ_CONNECTION|OAUTH_CLIENT_ID|OAUTH_CLIENT_SECRET)=' .env
python -c "from waca_loop.org import exit_if_incomplete; exit_if_incomplete(); print('org.toml OK')"
python scripts/init_bigquery.py --dry-run | head -3
```

- 1行目で5行が表示され、`GCP_PROJECT`・`BQ_LOCATION`・`BQ_CONNECTION` が準備4 の値、OAuth の2つが
  `placeholder` であること。
- 2行目で `org.toml OK` と表示されること(`CHANGE-ME` が残っていると修正箇所が表示される)。
- 3行目で `-- ===== 01_terms.sql =====` と、導入先のプロジェクトIDを含む `CREATE` 文が表示されること。

データセット名(`DS_*`)は既定値(`waca_loop_grounding` など)のままにしておくことを推奨する。本書の
コマンドは既定のデータセット名を前提にしている。

**うまくいかないとき**

- `Missing required config key(s): OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET` と表示される: `.env` の OAuth の
  2行が空である。上の `cp .env.example .env` からやり直す。
- `.env` をシェルに読み込む必要があるとき(値の確認など)は `set -a; source .env; set +a` を使う。
  `OAUTH_SCOPES` のように空白を含む値は引用符で囲んであるため、そのまま読み込める。
- 解決しない場合は、次の文面で Claude に相談する(Claude のチャットに入力)。

  ```text
  WACA Loop の docs/SETUP_gcp.md の §0 を実行したところ、エラーになった。原因と次に実行するコマンドを教えてほしい。
  - 実行したコマンド: (ここに貼り付け)
  - 表示されたエラーの全文: (ここに貼り付け)
  - gcloud config list の結果: (ここに貼り付け)
  ```

## 1. API の有効化

BigQuery・Vertex AI に加え、Cloud Run へのデプロイに使う Cloud Run・Cloud Build・Artifact Registry・
Secret Manager の API を有効にする。

**ターミナルで実行**:

```bash
gcloud services enable bigquery.googleapis.com bigqueryconnection.googleapis.com aiplatform.googleapis.com run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
```

新しいプロジェクトでは、ソースからのビルドに使われる既定のサービスアカウント(Compute Engine の既定の
サービスアカウント)に、ビルドに必要な `roles/run.builder` が付いていない場合がある。付いていなければ付与する。

**ターミナルで実行**:

```bash
gcloud projects get-iam-policy "$GCP_PROJECT" --flatten=bindings --filter="bindings.role:roles/run.builder" --format='value(bindings.members)'
```

上のコマンドで `serviceAccount:<プロジェクト番号>-compute@developer.gserviceaccount.com` が表示されなければ、
次を実行する。

**ターミナルで実行**:

```bash
gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/run.builder" --condition=None
```

**確認方法**: 次のコマンドで、上の7つの API が一覧に含まれること。`roles/run.builder` の確認コマンドを
もう一度実行し、既定のサービスアカウントが表示されること。

```bash
gcloud services list --enabled --format='value(config.name)' | grep -E 'bigquery|aiplatform|run|cloudbuild|artifactregistry|secretmanager'
```

**うまくいかないとき**

- `Billing account ... is not found` や `FAILED_PRECONDITION` で課金に関するエラーになる: プロジェクトの
  課金が有効でない。準備5 を確認する。
- `Service account ...-compute@developer.gserviceaccount.com does not exist` と表示される: Compute Engine の
  API が一度も有効になっていない。`gcloud services enable compute.googleapis.com` を実行してから付与し直す。
- 解決しない場合は、次の文面で Claude に相談する(Claude のチャットに入力)。

  ```text
  WACA Loop の docs/SETUP_gcp.md の §1 を実行したところ、エラーになった。原因と次に実行するコマンドを教えてほしい。
  - 実行したコマンド: (ここに貼り付け)
  - 表示されたエラーの全文: (ここに貼り付け)
  - gcloud config list の結果: (ここに貼り付け)
  ```

## 2. BigQuery から Vertex AI への接続の作成(埋め込みモデル用、SPEC_phase1 §2.3)

接続を作成し、その接続のサービスアカウントに Vertex AI の利用権限を付与する。接続の参照名
(`$BQ_CONNECTION`)は §0 で `.env` に書き込み済みである。

**ターミナルで実行**:

```bash
bq mk --connection --project_id="$GCP_PROJECT" --location="$BQ_LOCATION" --connection_type=CLOUD_RESOURCE bq_vertex
export CONN_SA="$(bq show --format=json --connection "$BQ_CONNECTION" | python -c "import sys,json;print(json.load(sys.stdin)['cloudResource']['serviceAccountId'])")"
echo "$CONN_SA"
gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
  --member="serviceAccount:${CONN_SA}" --role="roles/aiplatform.user" --condition=None
```

**確認方法**: `echo "$CONN_SA"` で `bqcx-<数字>-xxxx@gcp-sa-bigquery-condel.iam.gserviceaccount.com` の形の
アドレスが表示され、最後のコマンドの出力に `role: roles/aiplatform.user` が含まれること。

**うまくいかないとき**

- `bq mk` で `Already Exists` と表示される: 接続は作成済みである。2行目から続ける。
- `echo "$CONN_SA"` が空、または `KeyError` になる: 接続の場所が `$BQ_LOCATION` と異なる。
  `bq ls --connection --project_id="$GCP_PROJECT" --location="$BQ_LOCATION"` で `bq_vertex` が表示されるか確認する。
- 解決しない場合は、次の文面で Claude に相談する(Claude のチャットに入力)。

  ```text
  WACA Loop の docs/SETUP_gcp.md の §2 を実行したところ、エラーになった。原因と次に実行するコマンドを教えてほしい。
  - 実行したコマンド: (ここに貼り付け)
  - 表示されたエラーの全文: (ここに貼り付け)
  - gcloud config list の結果: (ここに貼り付け)
  ```

## 3. データセット・テーブル・埋め込みモデルの作成

1行目で実行される DDL を確認し、2行目でデータセットを作成して `sql/ddl/*.sql` を順に適用する。
データセットは `$BQ_LOCATION` の場所に作られる。

**ターミナルで実行**:

```bash
python scripts/init_bigquery.py --dry-run | head -3
python scripts/init_bigquery.py
```

`terms`、`persons`、`usage_log`(日付パーティション)、`oauth_identity_map`、ベクトルインデックス、
`06_embedding_model.sql`(`CREATE MODEL ... REMOTE WITH CONNECTION`)などが順に適用される。
手順2の接続が存在しないと `06` は失敗する。

> **注記(`05_vector_index.sql` の警告は正常)**: 実行中に
> `WARN: skipped 05_vector_index.sql … BadRequest: 400 … all NULLs` と表示される。ベクトルインデックスは
> 埋め込みの入った行がないと作れないため、データ投入前のこの時点では作成を見送っている。正常な動作で
> あり、対処は不要である(行が少ない間はインデックスなしで検索できる)。

**確認方法**: `dataset ready:` が5行(`waca_loop_grounding`・`waca_loop_ledger`・`waca_loop_feedback`・
`waca_loop_knowledge`・`waca_loop_integration`)表示され、最後に `done.` と表示されること。続けて次を実行する。

```bash
bq ls --project_id="$GCP_PROJECT"
```

上の5つのデータセットが表示されればよい。もう一度実行しても重複は作られない。

**うまくいかないとき**

- `Access Denied` や `403` になる: ADC のログインか課金先の設定が済んでいない。準備6 をやり直す。
- `06_embedding_model.sql` で `Not found: Connection` になる: §2 の接続がない、または場所が `$BQ_LOCATION`
  と異なる。§2 を確認する。
- `Dataset ... was not found in location ...` になる: 以前に別の場所で作ったデータセットが残っている。
  `.env` の `BQ_LOCATION` を確認する(準備3)。
- 解決しない場合は、次の文面で Claude に相談する(Claude のチャットに入力)。

  ```text
  WACA Loop の docs/SETUP_gcp.md の §3 を実行したところ、エラーになった。原因と次に実行するコマンドを教えてほしい。
  - 実行したコマンド: (ここに貼り付け)
  - 表示されたエラーの全文: (ここに貼り付け)
  - gcloud config list の結果: (ここに貼り付け)
  ```

## 4. 用語・ポリシーの投入と埋め込みの生成(SPEC_phase1 §2.3)

組織の用語と AI ポリシーの行を投入する。まず動作を確認する場合は、`sample/` にある架空の組織の
見本データを使用できる。

**ターミナルで実行**:

```bash
python scripts/seed_terms.py sample/terms.json
python scripts/seed_policy.py sample/policy.json
```

続いて用語の埋め込みを生成する(1行目で対象を確認し、2行目で `ML.GENERATE_EMBEDDING` により
`terms.embedding` を埋める)。

**ターミナルで実行**:

```bash
python scripts/backfill_embeddings.py --dry-run
python scripts/backfill_embeddings.py
```

**確認方法** — ターミナルで実行:

```bash
bq query --project_id="$GCP_PROJECT" --location="$BQ_LOCATION" --use_legacy_sql=false "SELECT COUNT(*) AS terms, COUNTIF(embedding IS NOT NULL AND ARRAY_LENGTH(embedding) > 0) AS embedded FROM \`${GCP_PROJECT}.waca_loop_grounding.terms\`"
```

見本データの場合、`terms` と `embedded` がどちらも `5` になればよい。もう一度投入しても重複しない。

**うまくいかないとき**

- 埋め込みの生成で `Permission denied` や `aiplatform` を含むエラーになる: §2 の `roles/aiplatform.user` の
  付与から数分たっていない可能性がある。数分待ってから2行目を再実行する。
- 解決しない場合は、次の文面で Claude に相談する(Claude のチャットに入力)。

  ```text
  WACA Loop の docs/SETUP_gcp.md の §4 を実行したところ、エラーになった。原因と次に実行するコマンドを教えてほしい。
  - 実行したコマンド: (ここに貼り付け)
  - 表示されたエラーの全文: (ここに貼り付け)
  - gcloud config list の結果: (ここに貼り付け)
  ```

## 5. OAuth 同意画面とローカル用クライアント(SPEC_phase1 §5)

### 5.1 OAuth 同意画面の設定(必須)

新しいプロジェクトでは、OAuth クライアントを作る前に同意画面(Google Auth Platform)の設定が必要である。
§8.1 のサーバー用クライアントも、この設定がないと作れない。

**ブラウザで操作**(画面上部のプロジェクト選択欄が導入先であることを先に確認する):

1. Google Cloud コンソール → **Google Auth Platform**(「API とサービス」→「OAuth 同意画面」からも開ける)
   → **開始**(または「ブランディング」)。
2. **アプリ情報**: アプリ名(例: `WACA Loop`)とユーザーサポートメール(管理者のアドレス)を入力する。
3. **対象**(対象ユーザー): 次のどちらかを選ぶ。
   - **内部**: 利用者が全員、組織の Google Workspace のアカウントで利用する場合。Workspace の組織に属する
     プロジェクトでのみ選べる。
   - **外部**: Workspace 外のアカウント(gmail.com など)で利用する人がいる場合、またはプロジェクトが
     Workspace の組織に属していない場合。公開ステータスは「テスト」のままでよい。
4. **連絡先情報**: 管理者のアドレスを入力し、ポリシーに同意して **作成** を選ぶ。
5. 「外部」を選んだ場合は、**対象** → **テストユーザー** → **ユーザーを追加** で、利用者全員の Google
   アカウント(承認画面で選ぶアドレス)を追加する(テストモードでは100人まで)。

要求するスコープは `openid email`(ローカル実行ではこれに BigQuery が加わる)のみで、Google の機密スコープの
審査は不要である。

**確認方法**: Google Auth Platform の「概要」(または「ブランディング」)にアプリ名が表示され、「対象」に
選んだユーザーの種類(内部/外部)が表示されること。外部の場合は、テストユーザーの一覧に利用者が並んでいること。

### 5.2 ローカル実行用のクライアント(任意。§7 を行う場合のみ)

§7 のローカル(stdio)での動作確認を行う場合だけ、デスクトップ アプリ用のクライアントを作り、`.env` の
仮の値を差し替える。Cloud Run だけを使う場合は飛ばしてよい(`.env` の `placeholder` はそのままでよい)。

**ブラウザで操作**:

1. Google Auth Platform → **クライアント** → **クライアントを作成** → アプリケーションの種類
   **デスクトップ アプリ** → 作成。
2. 表示されたクライアント ID とクライアント シークレットを控える。

**ターミナルで実行**(1行ずつ。シークレットの入力は画面に表示されない):

```bash
printf 'デスクトップ用の client ID: '; read DESKTOP_CLIENT_ID
printf 'デスクトップ用のシークレット: '; read -s DESKTOP_CLIENT_SECRET; echo
```

**ターミナルで実行**:

```bash
python - "OAUTH_CLIENT_ID=${DESKTOP_CLIENT_ID}" "OAUTH_CLIENT_SECRET=${DESKTOP_CLIENT_SECRET}" <<'EOF'
import pathlib
import sys
updates = dict(arg.split("=", 1) for arg in sys.argv[1:])
path = pathlib.Path(".env")
lines = []
for line in path.read_text(encoding="utf-8").splitlines():
    key = line.split("=", 1)[0].strip()
    if key in updates:
        line = f"{key}={updates.pop(key)}"
    lines.append(line)
lines += [f"{key}={value}" for key, value in updates.items()]
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("updated .env")
EOF
unset DESKTOP_CLIENT_SECRET
```

**確認方法**: `grep '^OAUTH_CLIENT_ID=' .env` で、`placeholder` ではなく `〜.apps.googleusercontent.com` で
終わる値が表示されること。スコープは `.env.example` に設定済みである
(`https://www.googleapis.com/auth/bigquery openid email`)。`waca-loop` を初めて実行すると同意画面が開き、
トークンが `OAUTH_TOKEN_STORE` に保存される(`src/waca_loop/auth/oauth.py`)。

## 6. 利用者の登録(SPEC_phase1 §5)

通常は、利用者を登録したうえで**メールアドレスで事前登録**する。各利用者の Google の `sub` は、
初回ログイン時に自動で紐づけられる。登録は名簿ファイル(`python scripts/register_members.py members.json`。
`members.example.json` と `sample/` を参照)または組織シートから行う。手順は
[MANUAL_users.md §5.1](MANUAL_users.md#51-利用者の登録) を参照。名簿に登録するのは、利用者が承認画面で
選ぶ **Google アカウントのメールアドレス**である(Claude にログインしているアカウントとは別でもよい)。

**確認方法** — ターミナルで実行:

```bash
bq query --project_id="$GCP_PROJECT" --location="$BQ_LOCATION" --use_legacy_sql=false "SELECT person_id, email, oauth_sub FROM \`${GCP_PROJECT}.waca_loop_grounding.oauth_identity_map\` ORDER BY created_at DESC LIMIT 20"
```

登録した利用者が `oauth_sub` が `pending:` で始まる行として表示されればよい(初回ログイン後は実際の
`sub` に置き換わる)。

既知の `sub` を手動で紐づける場合は、`oauth_identity_map` に行を追加する。これにより台帳の行が
`actor_id` に解決される(紐づけのない利用者はハッシュ化した `actor_raw` で記録される)。値は次の3行で
1行ずつ入力してから、続くコマンドを実行する。

**ターミナルで実行**(1行ずつ):

```bash
printf 'Google の sub: '; read MAP_SUB
printf 'person_id: '; read MAP_PERSON_ID
printf 'メールアドレス: '; read MAP_EMAIL
```

**ターミナルで実行**:

```bash
bq query --project_id="$GCP_PROJECT" --location="$BQ_LOCATION" --use_legacy_sql=false \
  --parameter="sub::${MAP_SUB}" --parameter="pid::${MAP_PERSON_ID}" --parameter="email::${MAP_EMAIL}" \
  "INSERT INTO \`${GCP_PROJECT}.waca_loop_grounding.oauth_identity_map\` (oauth_sub, person_id, email, created_at, updated_at) VALUES (@sub, @pid, @email, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())"
```

**確認方法**: `Number of affected rows: 1` と表示されること。

## 7. ローカルでの動作確認(任意)

§5.2 でデスクトップ用クライアントを設定した場合に、MCP サーバーを stdio で起動し、AI クライアントから
接続して確かめられる。接続方法は [CONNECT.md](CONNECT.md) を参照。Cloud Run だけを使う場合は飛ばして §8 へ進む。

**ターミナルで実行**:

```bash
waca-loop
```

投入した用語を `lookup_terms` で検索し、`log_usage` で `usage_log` に行が書き込まれることを確認する。

> **補足(検索時の埋め込み)**: `EMBEDDING_PROVIDER=bq_ml` の場合、検索語の埋め込みも BigQuery 内で
> 生成される。`BigQueryTermStore.semantic_candidates` が `VECTOR_SEARCH` の中で
> `ML.GENERATE_EMBEDDING` を実行する(Python 側でベクトルを扱わない)。このため、埋め込みモデル
> (手順3)と Vertex AI への接続(手順2)が必要である。オフラインのテストでは決定的な
> `HashingEmbedder` を使用する。

## 8. Cloud Run へのデプロイ(リモート MCP サーバー、SPEC_phase1 §3.3・§5)

手順1〜7 と異なり、サーバーをインターネットに公開する。専用の OAuth クライアントと IAM の設定が必要で
ある。各コマンドは内容を確認したうえで実行すること。

**構成**: サーバー自身が OAuth 認可サーバーとなり、動的クライアント登録(DCR)の背後で
Google ログインを仲介する(`src/waca_loop/auth/as_provider.py`)。Claude は Google と直接やり取り
しない。Claude はサーバーの `/register` に自動登録し、サーバーの `/authorize` がブラウザを Google に
転送する。`/auth/google/callback` で Google の ID トークンを検証し、`oauth_identity_map` の許可リスト
(SPEC_phase1 §5)を確認したうえで、サーバーが独自のアクセストークンを発行する。このため、利用者に
クライアントシークレットを配布する必要はなく、利用者は「URL を追加して1回承認する」だけで接続できる。
Google は DCR に対応していないため、Claude から Google を直接の認可サーバーとして使うことはできない。

**デプロイは1回で済む。** Cloud Run の URL は `https://<サービス名>-<プロジェクト番号>.<リージョン>.run.app`
の形でデプロイ前に決まるため、準備4 で `MCP_PUBLIC_URL` として組み立て済みである。これを使って先に
OAuth クライアントのリダイレクト URI を登録し(8.1)、権限と秘密の値を用意してから(8.2・8.3)、
`MCP_PUBLIC_URL` を含めて1回でデプロイする。

### 8.1 Web アプリケーション用の OAuth クライアント(§5.2 のデスクトップ用は流用しない)

デスクトップ アプリ用の OAuth クライアントは `localhost` へのリダイレクトしか登録できず、HTTPS の
コールバックを登録できない。公開サーバーには専用の **ウェブ アプリケーション** クライアントが必要である。
§5.1 の同意画面を先に設定しておくこと。

まず、登録するリダイレクト URI を表示する。

**ターミナルで実行**:

```bash
echo "${MCP_PUBLIC_URL}/auth/google/callback"
```

`https://waca-loop-serve-<プロジェクト番号>.<リージョン>.run.app/auth/google/callback` の形で表示される。

**ブラウザで操作**(画面上部のプロジェクト選択欄が導入先であることを先に確認する):

1. Google Auth Platform → **クライアント** → **クライアントを作成** → アプリケーションの種類
   **ウェブ アプリケーション**。名前は任意(例: `waca-loop-serve`)。
2. **承認済みのリダイレクト URI** に、上で表示された URI をそのまま貼り付ける(`claude.ai/...` ではない。
   この構成では Claude ではなくサーバーが Google の OAuth クライアントとなる)。「承認済みの JavaScript
   生成元」は空のままでよい。作成する。
3. 表示された **クライアント ID** と **クライアント シークレット** を、すぐ後の 8.3 で Secret Manager に
   保存する。この構成ではサーバー側で Google と認可コードを交換するため、シークレットが必要である。
   `client_id` は `verify_google_token` での ID トークンの `aud` 検証にも使われる。

**確認方法**: クライアントの詳細画面で、承認済みのリダイレクト URI が上の `echo` の表示と1文字も違わない
こと。サーバーが Google に渡す URI と完全に一致しない場合、Google は `redirect_uri_mismatch` を返す。

> **OAuth 同意画面**: 公開・検証が済むまでは、同意画面で許可したユーザーしかアクセスできない。
> Workspace 外のアカウント(gmail.com など)で利用する利用者がいる場合は、同意画面を「外部」にして
> **テストユーザー**に追加する(§5.1)。

### 8.2 サービスアカウントと BigQuery の権限(SPEC_phase1 §3.3。アクセス制御はサーバーのみが行う)

サーバー用のサービスアカウント(`$SA`)を作成し、次の権限を付与する。

- 4つのデータセット(grounding / ledger / feedback / knowledge)の `dataEditor`(プロジェクト全体ではなく、
  データセット単位で付与する)
- プロジェクトの `roles/bigquery.jobUser`(クエリの実行)
- プロジェクトの `roles/bigquery.connectionUser`(§2 の接続の利用。これがないと検索時の埋め込みの生成が
  `bigquery.connections.use` の権限不足(403)で失敗し、`lookup_terms` が動かない。構築時の埋め込みの生成は
  管理者の権限で動くため、構築中には気づけない)

**ターミナルで実行**(サービスアカウントの作成):

```bash
gcloud iam service-accounts create "$SERVICE" --display-name="waca-loop hosted MCP (Cloud Run)"
```

**ターミナルで実行**(データセット単位の権限。BigQuery の SQL の `GRANT` 文で付与する):

```bash
for DS in waca_loop_grounding waca_loop_ledger waca_loop_feedback waca_loop_knowledge; do
  bq query --project_id="$GCP_PROJECT" --location="$BQ_LOCATION" --use_legacy_sql=false \
    "GRANT \`roles/bigquery.dataEditor\` ON SCHEMA \`${GCP_PROJECT}.${DS}\` TO \"serviceAccount:${SA}\""
done
```

`bq add-iam-policy-binding` は「This feature requires allowlisting.」で失敗するため使わない。なお `bq` は
エラーを標準出力に出すので、出力を捨てずに(`> /dev/null` などを付けずに)実行し、エラーがないことを見る。

**ターミナルで実行**(プロジェクト単位の権限):

```bash
gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
  --member="serviceAccount:${SA}" --role="roles/bigquery.jobUser" --condition=None
gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
  --member="serviceAccount:${SA}" --role="roles/bigquery.connectionUser" --condition=None
```

**確認方法** — ターミナルで実行:

```bash
for DS in waca_loop_grounding waca_loop_ledger waca_loop_feedback waca_loop_knowledge; do
  echo "${DS}: $(bq show --format=prettyjson "${GCP_PROJECT}:${DS}" | grep -c "$SA")"
done
gcloud projects get-iam-policy "$GCP_PROJECT" --flatten=bindings --filter="bindings.members:serviceAccount:${SA}" --format='value(bindings.role)'
```

- 前半で4つのデータセットそれぞれに `1` 以上の数が表示されること(`0` はサービスアカウントが含まれていない)。
- 後半で `roles/bigquery.connectionUser` と `roles/bigquery.jobUser` の2行が表示されること。

**うまくいかないとき**

- `gcloud iam service-accounts create` で `already exists` と表示される: 作成済みである。次へ進む。
- `GRANT` 文がエラーになる、または確認で `0` が表示される: 代わりに、データセットのアクセス設定
  (access entries)に WRITER として追加する。次をそのまま実行する(何度実行しても重複しない)。

  **ターミナルで実行**:

  ```bash
  python - <<'EOF'
  import os
  from google.cloud import bigquery
  project = os.environ["GCP_PROJECT"]
  sa = os.environ["SA"]
  client = bigquery.Client(project=project, location=os.environ["BQ_LOCATION"])
  for name in ["waca_loop_grounding", "waca_loop_ledger", "waca_loop_feedback", "waca_loop_knowledge"]:
      dataset = client.get_dataset(f"{project}.{name}")
      entries = list(dataset.access_entries)
      if any(e.entity_id == sa for e in entries):
          print(f"already granted: {name}")
          continue
      entries.append(bigquery.AccessEntry(role="WRITER", entity_type="userByEmail", entity_id=sa))
      dataset.access_entries = entries
      client.update_dataset(dataset, ["access_entries"])
      print(f"granted WRITER: {name}")
  EOF
  ```

  4行の `granted WRITER:` または `already granted:` が表示された後、上の確認方法をもう一度実行する。
- 解決しない場合は、次の文面で Claude に相談する(Claude のチャットに入力)。

  ```text
  WACA Loop の docs/SETUP_gcp.md の §8.2 を実行したところ、エラーになった。原因と次に実行するコマンドを教えてほしい。
  - 実行したコマンド: (ここに貼り付け)
  - 表示されたエラーの全文: (ここに貼り付け)
  - gcloud config list の結果: (ここに貼り付け)
  ```

### 8.3 秘密の値の保存とデプロイ

**`--max-instances=1 --min-instances=1` を指定すること。** アクセストークンとリフレッシュトークンは
HMAC 署名による自己完結型(`auth/tokens.py`、鍵は `MCP_TOKEN_SIGNING_KEY`)で、動的クライアント登録の
情報は `oauth_clients` テーブルに保存される(`auth/client_store.py`)。このため再起動や再デプロイで
接続が切れることはない。ただし、ログイン処理中の一時的な状態(保留中の `/authorize` 要求と一回限りの
コード。数秒で消える)はメモリ上に保持されるため、インスタンスは1つに限定する。再起動の瞬間に進行中の
ログインが失敗することはあるが(再試行で成功する)、確立済みの接続には影響しない。`min=1` は、
OAuth のリダイレクト途中でコールドスタートが起きることを防ぐ(そのぶん、使わない時間も費用がかかる。§9)。

**`MCP_TOKEN_SIGNING_KEY` は、任意の利用者になりすませる鍵である。** 署名鍵を知る者はトークンを自由に
発行でき、アクセス制御をすべて迂回できる。必ず Secret Manager 経由で渡し(下のコマンドは
`--set-secrets` を使用する)、平文の `--set-env-vars`・シェルの履歴・`.env` の写しに置かないこと。
8.1 の OAuth クライアントのシークレットも同じく Secret Manager に保存し、`--set-secrets` で渡す。
鍵は無停止で切り替えられる。値を `新しい鍵,古い鍵` としてデプロイし(先頭の鍵で発行し、すべての鍵で
検証する)、リフレッシュトークンの有効期間である30日が経過した後に古い鍵を外す。鍵を1つだけ即時に
差し替えると、全員が一斉にログアウトされる(インシデント時の最終手段)。

前提: `sql/ddl/10_oauth_clients.sql` が適用済みであること(§3 の `python scripts/init_bigquery.py` で適用される)。

**(1) 署名鍵**を1回だけ生成し、Secret Manager にのみ保存する。

**ターミナルで実行**:

```bash
openssl rand -base64 48 | gcloud secrets create mcp-token-signing-key --data-file=-
gcloud secrets add-iam-policy-binding mcp-token-signing-key \
  --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" --condition=None
```

**(2) 8.1 の OAuth クライアント**の ID とシークレットを Secret Manager に保存する。次の2行を**1行ずつ**
貼り付けて入力する(シークレットの入力は画面に表示されない)。

**ターミナルで実行**(1行ずつ):

```bash
printf 'Web クライアントの client ID: '; read WEB_CLIENT_ID
printf 'Web クライアントのシークレット: '; read -s WEB_CLIENT_SECRET; echo
```

**ターミナルで実行**:

```bash
printf '%s' "$WEB_CLIENT_ID" | gcloud secrets create oauth-client-id --data-file=-
printf '%s' "$WEB_CLIENT_SECRET" | gcloud secrets create oauth-client-secret --data-file=-
unset WEB_CLIENT_SECRET
for SECRET in oauth-client-id oauth-client-secret; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" --condition=None
done
```

**(3) 組織設定 `org.toml`** はコンテナイメージに含まれない(git の管理対象外のため)。Secret Manager に
保存し、ファイルとしてマウントする。サーバーは `ORG_CONFIG` で指定した場所から読み込む。

**ターミナルで実行**:

```bash
gcloud secrets create org-config --data-file=org.toml
gcloud secrets add-iam-policy-binding org-config \
  --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" --condition=None
```

`org.toml` を変更した場合は、`gcloud secrets versions add org-config --data-file=org.toml` を実行してから
再デプロイする。

**確認方法**: 次のコマンドで `mcp-token-signing-key`・`oauth-client-id`・`oauth-client-secret`・`org-config`
の4つが表示されること。

```bash
gcloud secrets list --format='value(name)'
```

**(4) デプロイ**(1回で完了する)。ソースからのビルドに使う Artifact Registry のリポジトリを先に作って
おくと、デプロイの途中で確認の質問(Y/n)が出ない。

**ターミナルで実行**:

```bash
gcloud artifacts repositories create cloud-run-source-deploy --repository-format=docker --location="$REGION"
```

**ターミナルで実行**:

```bash
gcloud run deploy "$SERVICE" \
  --source . --region "$REGION" --service-account "$SA" \
  --allow-unauthenticated --max-instances=1 --min-instances=1 \
  --set-env-vars="GCP_PROJECT=${GCP_PROJECT},BQ_LOCATION=${BQ_LOCATION},BQ_CONNECTION=${BQ_CONNECTION},EMBEDDING_PROVIDER=bq_ml,EMBEDDING_MODEL=text-multilingual-embedding-002,BQ_AUTH=adc,ORG_CONFIG=/etc/waca-loop/org.toml,MCP_PUBLIC_URL=${MCP_PUBLIC_URL}" \
  --set-secrets="OAUTH_CLIENT_ID=oauth-client-id:latest,OAUTH_CLIENT_SECRET=oauth-client-secret:latest,MCP_TOKEN_SIGNING_KEY=mcp-token-signing-key:latest,/etc/waca-loop/org.toml=org-config:latest"
```

`BQ_LOCATION` には、データセットを作った場所と同じ値(`$BQ_LOCATION`)が渡される(準備3)。

**確認方法**: 数分後に `Service [waca-loop-serve] revision [...] has been deployed and is serving 100 percent
of traffic.` と表示されること。続けて、サービスの URL が `MCP_PUBLIC_URL` と一致することと、サーバーが
応答することを確かめる。

**ターミナルで実行**:

```bash
echo "$MCP_PUBLIC_URL"
gcloud run services describe "$SERVICE" --region "$REGION" --format=json | grep -o 'https://[^"]*run\.app' | sort -u
curl -s "${MCP_PUBLIC_URL}/.well-known/oauth-authorization-server" | head -c 300; echo
```

- 2行目の一覧に、1行目と同じ URL が含まれること(`https://waca-loop-serve-xxxxxxxx-an.a.run.app` の形の
  URL も併せて表示されることがあるが、どちらも同じサービスを指す。WACA Loop では `MCP_PUBLIC_URL` の
  ほうを使う)。
- 3行目で `{"issuer":"https://waca-loop-serve-<プロジェクト番号>.<リージョン>.run.app/"` で始まる JSON が
  表示されること。

`--allow-unauthenticated` は Cloud Run の入口の制御(HTTPS で到達できるかどうか)であり、アプリケーション
側の認証を迂回するものではない。ツールのエンドポイント `/mcp` は MCP の認証ミドルウェアで保護され、
サーバーが発行したトークンのみが通る。トークンは、Google ログインと `oauth_identity_map` による確認
(`auth/as_provider.py`)を通過した後にのみ発行される。OAuth のエンドポイント(`/authorize`、`/token`、
`/register`、`/auth/google/callback`、`.well-known/*`)は意図的に公開している。

**うまくいかないとき**

- `gcloud artifacts repositories create` で `ALREADY_EXISTS` と表示される: 作成済みである。次へ進む。
- `gcloud secrets create` で `already exists` と表示される: 作成済みである。値を入れ直す場合は `create` の
  代わりに `gcloud secrets versions add <シークレット名> --data-file=-` を使う。
- ビルドが `PERMISSION_DENIED` で失敗する: 既定のサービスアカウントに `roles/run.builder` がない。§1 の
  付与を行ってから、デプロイを再実行する。
- デプロイ後のリビジョンが起動しない(`Ready condition status changed to False`): ログを確認する。
  `gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\"" --limit=50 --freshness=1h --format='value(textPayload)'`。
  `Refusing to start` が出ている
  場合は、表示された設定(`MCP_PUBLIC_URL`・`MCP_TOKEN_SIGNING_KEY` など)が渡っていない。
  `組織設定(org.toml)を確認してください` が出ている場合は、`org-config` の中身に `CHANGE-ME` が残っている。
- 2行目の一覧に `MCP_PUBLIC_URL` が含まれない: 準備4 の `REGION` か `PROJECT_NUMBER` が誤っている。
  `cat .env.setup` で確認し、準備4 からやり直したうえで 8.1 のリダイレクト URI を直し、再デプロイする。
- 解決しない場合は、次の文面で Claude に相談する(Claude のチャットに入力)。

  ```text
  WACA Loop の docs/SETUP_gcp.md の §8.3 を実行したところ、エラーになった。原因と次に実行するコマンドを教えてほしい。シークレットと署名鍵の値は伏せてある。
  - 実行したコマンド: (ここに貼り付け)
  - 表示されたエラーの全文: (ここに貼り付け)
  - gcloud config list の結果: (ここに貼り付け)
  ```

### 8.4 動作確認

- 接続に使う URL を表示する(ターミナルで実行: `echo "${MCP_PUBLIC_URL}/mcp"`)。claude.ai または
  Claude Desktop で、この URL をカスタムコネクタとして追加する(利用者向けの手順は
  [MANUAL_users.md §2](MANUAL_users.md#2-導入手順))。末尾の `/mcp` は必須である(claude.ai は入力した URL を
  そのまま MCP エンドポイントとして使い、`/mcp` を補完しない。ベース URL のみの場合、OAuth は通るがその後
  `POST / → 404` で失敗する)。Claude が自動登録(DCR)を行い、ブラウザで Google のログイン画面が開くので
  承認する。
- `oauth_identity_map` に登録されていないアカウントでは「登録されていません。管理者に連絡してください」の
  画面が表示され、トークンは発行されない(Cloud Run のログに `rejected unregistered <ハッシュ化した sub> email=...` が記録される)。
  手順6で登録してから再接続する。
- 登録済みのアカウントでは、WACA Loop のツール(`lookup_terms`・`get_applicable_policy`・`log_usage` など)
  が使用できる。Claude のチャットで、§4 で投入した用語を名指しして質問し(例:「<組織名>の定例研修とは」)、
  登録どおりの定義が返ることを確かめる。`lookup_terms` が権限エラー(403、`bigquery.connections.use`)に
  なる場合は、§8.2 の `roles/bigquery.connectionUser` の付与を確認する。
- 再起動後も接続が維持されることは、次のコマンドで再起動を発生させて確認できる(ターミナルで実行)。

  ```bash
  gcloud run services update "$SERVICE" --region "$REGION" --update-labels=restart=$(date +%s)
  ```

## 9. 費用と後片付け

### 9.1 費用

Cloud Run は `--min-instances=1` のため、**利用者がいなくても常に1台が起動しており、費用がかかる**
(既定の CPU・メモリの設定で、目安として月に10米ドル前後。料金は変わることがあるため、コンソールの
「お支払い」→「レポート」で実際の額を確認する)。このほか、BigQuery の保存・クエリ、Vertex AI の
埋め込みの生成、Secret Manager に少額の費用がかかる。

### 9.2 予算アラートの設定

想定外の請求に気づけるよう、予算アラートを設定しておく。

**ブラウザで操作**: Google Cloud コンソール → **お支払い** → **予算とアラート** → **予算を作成** →
対象のプロジェクトに導入先を選び、金額(例: 月3,000円)と通知のしきい値(例: 50%・90%・100%)を設定する。

コマンドで設定する場合は次のとおり(請求先アカウントの管理権限が必要)。金額は請求先アカウントの通貨で
入力する(例: `3000JPY`、`20USD`)。

**ターミナルで実行**(1行ずつ):

```bash
printf '予算額 (例 3000JPY): '; read BUDGET_AMOUNT
```

**ターミナルで実行**:

```bash
export BILLING_ACCOUNT="$(gcloud billing projects describe "$GCP_PROJECT" --format='value(billingAccountName)' | sed 's|billingAccounts/||')"
gcloud services enable billingbudgets.googleapis.com
gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" \
  --display-name="waca-loop ${GCP_PROJECT}" --budget-amount="$BUDGET_AMOUNT" \
  --filter-projects="projects/${PROJECT_NUMBER}" \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
```

**確認方法**: `gcloud billing budgets list --billing-account="$BILLING_ACCOUNT"` で、作成した予算が表示されること。

### 9.3 検証後の削除

**削除は取り消しにくい操作である。** 実行する前に、`gcloud config get-value project` で対象が導入先の
プロジェクトであることを必ず確かめる。

**A. 検証専用に作ったプロジェクトの場合** — プロジェクトごと削除する。Cloud Run・BigQuery・Secret Manager
などがまとめて消え、以後の課金も止まる(削除後30日間は復元できる)。

**ターミナルで実行**:

```bash
gcloud projects delete "$GCP_PROJECT"
```

**B. 既存のプロジェクトに追加した場合** — プロジェクトは残し、WACA Loop が作ったものだけを削除する。
データセットの削除で、用語・名簿・台帳のデータはすべて消える。

**ターミナルで実行**:

```bash
gcloud run services delete "$SERVICE" --region "$REGION"
for DS in waca_loop_grounding waca_loop_ledger waca_loop_feedback waca_loop_knowledge waca_loop_integration; do
  bq rm -r -d "${GCP_PROJECT}:${DS}"
done
bq rm --connection "$BQ_CONNECTION"
for SECRET in mcp-token-signing-key oauth-client-id oauth-client-secret org-config; do
  gcloud secrets delete "$SECRET"
done
gcloud iam service-accounts delete "$SA"
```

各コマンドは削除の前に確認(y/N)を求めるので、対象を確かめて `y` を入力する。

**どちらの場合も**、9.2 で作った予算アラート、OAuth クライアント(B の場合)、手元の `members.json`・
`.env`・`.env.setup`、Claude に追加したコネクタを、不要になれば削除する。

**確認方法**: A の場合は `gcloud projects describe "$GCP_PROJECT" --format='value(lifecycleState)'` で
`DELETE_REQUESTED` と表示されること。B の場合は `gcloud run services list --region "$REGION"` と
`bq ls --project_id="$GCP_PROJECT"` に WACA Loop のサービスとデータセットが表示されないこと。
