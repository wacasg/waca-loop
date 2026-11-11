# WACA Loop — 実装仕様書(中核)

> WACA Loop の中核(用語・台帳・権限・ポリシー)の実装仕様である。SFAQ は SPEC_phase2.md、ERN は SPEC_phase3.md。
> 本リポジトリのコードは、この文書の節を `§X` の形で参照している。

## 0. 基本方針

- データ基盤：**既存または専用の BigQuery プロジェクトにデータセットを作る**。
- 認証：**Google OAuth**。リモート(ホスト型)ではサーバー自身が認可サーバーとなり Google ログインを仲介する
  (§5)。ローカル(stdio)ではインストール型アプリの標準的なフローを使う。
- 検索：**BigQuery `VECTOR_SEARCH`**（BM25相当の完全/部分一致と併用＝ハイブリッド）。
- 言語：**Python 基本**。速度要求部は **Rust 切り出し可**にするため、インターフェースを言語非依存に定義。

## 1. アーキテクチャ

### 1.1 設計意図

利用者は「いつもの AI クライアント」から 1 つの MCP サーバに接続する。サーバは
**(A) 組織の言葉を返す（terms 検索）**、**(B) AI 利用を記録する（層1台帳）**、
**(C) 出してよい情報だけ返す（権限フィルタ）** の 3 役。監視は台帳の副産物。

### 1.2 コンポーネント

```
AIクライアント ──(MCP)──► waca-loop MCP server (Python)
                            ├─ auth/        Google OAuth
                            ├─ grounding/   用語・人物の検索（ハイブリッド）
                            ├─ ledger/      層1台帳への要約記録
                            ├─ retrieval/   VECTOR_SEARCH＋BM25＋ACLフィルタ（速度要件→Rust候補）
                            ├─ signature/   4D署名のシステムプロンプト供給
                            └─ monitor/     ポジティブリスト監視（バッチ）
                                   │
                                   ▼
                        BigQuery（既存プロジェクト／新規データセット）
```

### 1.3 リポジトリ構成

リポジトリ全体の構成は [`../README.md`](../README.md) を参照。`src/waca_loop/` のサブパッケージ
(`auth`、`grounding`、`ledger`、`retrieval`、`signature`、`monitor`)に対応している。

## 2. BigQuery データモデル（DDL）

### 2.1 設計意図

- **カラム最小**。分類・タグは AI が付与し人が確定するため固定列にしない（`attributes` を JSON）。
- 各レコードに **スコープタグ3軸**（階層／対内外／機微度）を必須で持たせ、検索時に閲覧者権限で必ず絞る。
- 用語の**意味検索**のため `embedding ARRAY<FLOAT64>` を持ち、`VECTOR_SEARCH` で使う。

データセット：`<DS_GROUNDING>`（terms, persons）／`<DS_LEDGER>`（usage_log）。
DDL は [`../sql/ddl/`](../sql/ddl/) を参照（`01_terms.sql` 〜 `05_vector_index.sql`）。

### 2.2 テーブル

- ① `terms` — 用語テーブル（terms）。`01_terms.sql`
- ② `persons` — 人物テーブル（個人情報として分離）。`02_persons.sql`
- ③ `usage_log` — 層1 ガバナンス台帳（全件・軽い・要約JSON）。`PARTITION BY DATE(occurred_at)`。`03_usage_log.sql`
- `oauth_identity_map` — OAuth `sub` → `person_id` のマッピング（§5、個人情報分離）。`04_oauth_identity_map.sql`

**保持**：`usage_log` の行は自動では消さない（パーティションの有効期限を設定しない）。原文全文は層1に持たない（要約のみ）。

### 2.3 VECTOR_SEARCH 前提

- `terms.embedding` を使い、用語の意味検索を行う。
- 実装は2択：**(a) BQネイティブ** `CREATE VECTOR INDEX` ＋ `VECTOR_SEARCH()`、
  **(b) `ML.GENERATE_EMBEDDING`** `CREATE MODEL`（Vertex接続）。
- 埋め込み生成は `scripts/backfill_embeddings.py` のバックフィルで投入時に計算。
  検索時は `VECTOR_SEARCH` を呼ぶ（§3 `hybrid_search` 参照）。`05_vector_index.sql`。

## 3. 検索（retrieval）── ハイブリッド＋ACL

### 3.1 設計意図

「質問が何の用語に当たるか」を **BM25相当（完全/部分一致）＋ VECTOR_SEARCH（意味）** の併用で出し、
**閲覧者の権限で必ず絞る**。評価ではなくマッチングなのでベクトルは安全に使える。

### 3.2 インターフェース（言語非依存・Rust切り出し可）

```
hybrid_search(query: str, viewer: Viewer, k: int) -> list[TermHit]
  1. lexical  = bm25_like(query)              # LOWER/部分一致＋簡易スコア
  2. semantic = vector_search(embed(query))   # VECTOR_SEARCH top-k
  3. fused    = rrf(lexical, semantic)        # Reciprocal Rank Fusion
  4. return acl_filter(fused, viewer)         # ★必須：スコープ×閲覧者権限

acl_filter(hits, viewer):
  keep hit if visible_to(hit.scope_tier, hit.scope_audience, hit.scope_sensitivity, viewer)
```

### 3.3 ACL（権限を引き継ぐ検索）規則（最小）

- `scope_audience='internal'` は組織メンバーのみ。`partner` は取引先共有許可者。`public` は誰でも。
- `scope_sensitivity='sensitive'` は `owner_unit` 一致 or 明示許可がある閲覧者のみ。
- `scope_tier` は `personal`/`team`/`dept` のいずれも「記録の `owner_unit` に所属する閲覧者」、`org`=全社。
  `personal` を本人だけに絞るには、本人だけが所属するユニットを `owner_unit` にする。
- **`org` の行の `owner_unit` は判定に使われない**（`tier='org'` の時点で全社可視が確定する）。
  そこに入っているユニットは「この記録を**管理しているのは誰か**」であって権限ではない。
  例: `owner_unit='kyozai'` かつ `tier='org'` の `terms` の行は、教材部門が編んだ
  語彙が全社に見えている状態であり、これは意図どおりである。
  **「所有ユニットが書いてあるから他部門には見えない」と読んではならない。**
  部門限定にしたい行は `tier` を `dept` にすること。所有の記載だけでは何も締まらない。
- **フィルタは検索の最後でなく BQ クエリの WHERE 句にも二重で効かせる**（取りこぼし＝漏えいを構造的に防ぐ）。

## 4. MCP ツール定義

### 4.1 設計意図

クライアントは最小ツールだけを見る。**毎プロンプトで `log_usage` を呼ばせる**のが台帳の主トリガー
（要約を送る）。用語参照は `lookup_terms`。

### 4.2 ツール仕様

```
TOOL lookup_terms
  desc: 組織の用語・社内語・略語・人物を解決し、AIの誤読を防ぐ。
  input:  query: string,  k: int = 5
  behavior: hybrid_search → acl_filter（呼び出しユーザの権限で絞る）
            → 件数上限で切る（下記 FLOOR-02 の抽出量ゲート）
  output: hits: [{term, definition, basis, scope, kind: "term"|"person"}]

# 抽出量の上限（FLOOR-02「コア資産の丸ごと持ち出し」の実装）
#   ACL は「何が見えるか」を決める。これは「1コールでどれだけ出せるか」を決める別の問い。
#   k=100000 は権限境界を越えないが、部署のコーパスを丸ごと外部AIベンダのコンテキストへ
#   移送できてしまい、台帳に k は残らないので事後に k=5 と区別できない。
#   平常時上限: lookup_terms 20 / search_sfaq 20 / 人物 10 / find_expert 3 /
#               1問いあたりのフィードバック 20 / improvement_tasks 50 / improvement_requests 50。
#   超過は org.toml の [units] export_admin の閲覧者が理由を述べたときのみ許し、tool_events に
#   k_override を1行残す（監査行を書けなければ超過も許さない）。名簿(人物)は
#   最も狭く、超過の対象外。上限超過は例外にせず黙って切る（拒否するとページングで
#   迂回され、かえって痕跡が残らないため — §9 の助言姿勢と同じ）。

TOOL log_usage             # 毎プロンプトで必ず呼ぶ（システムプロンプトで強制）
  desc: 利用を層1台帳に記録（要約のみ・逐語送信しない）。ポリシー行に紐づけ、助言を返す（§9）。
  input:
    use_category: "l2"|"l1"|"l0"|"prohibited"   # L2=そのまま採用 / L1=条件つき /
                                                # L0=いまは使わない / prohibited=禁止列挙
                                                # （red/yellow/green の三色も受理し写像）
    data_kinds:   string[]   # pii|confidential|internal|public|core_asset|final_judgment|none
                             # ＋ 該当ポリシー行の trigger_kinds（R9）。語彙外の自由記述は
                             # internal へ寄せ（none ではない＝無害側に倒さない）、写せなかった
                             # 原文は unmapped_data_kinds として呼び手へ返す。台帳には正典語彙のみ
    summary:      string     # モデルが生成した要点要約
    client:       string     # claude_desktop|claude_code|chatgpt|gemini|other
    request_id:   string?    # 任意。**1回の記録の再送キー**。id 形
                             # ([A-Za-z0-9._:-]{1,64}) 以外は捨てる（自由文を台帳に
                             # 入れない）。同 actor・同 request_id・同内容の記録が
                             # 15分以内にあれば新しい行は書かず、既存 log_id を
                             # deduplicated:true と共に返す（証跡は usage_recorded では
                             # なく usage_resend_deduped）
    policy_row_ids: string[]?  # get_applicable_policy で該当した行ID（§9.3・任意）
  behavior: usage_log へ INSERT（occurred_at=now, actor=OAuth sub解決,
            policy_row_ids/policy_version を記録）→ classify_usage で助言を生成（§9.4）
  output: { log_id, suggested_category, mismatch, floor_warnings, deduplicated? }
          # 台帳書き込みが例外になったときは固定文 `WACA_LOOP_LEDGER_WRITE_FAILED: …` を送出する
          # （例外本文・traceback は外に出さない）

TOOL get_company_context   # 任意：用語＋直近の関連情報をまとめて返す薄いラッパ
  input:  { query: string }
  output: { context: string, used_terms: [...] }
```

**注意（MCPの限界）**：MCP は「モデルがツールを呼んだ時だけ」発火する。`log_usage` は確率的に
抜けうるため、システムプロンプトで強制（§6）＋ポジティブリスト監視（§7）＋Claude Code は Hook 併用で補う。

## 5. 認証（Google OAuth）

- `auth/oauth.py`：ローカル(stdio)用の OAuth クライアント設定（Client ID/Secret、scopes、token store）。
  スコープは BigQuery 読み書き（`https://www.googleapis.com/auth/bigquery`）＋ユーザ識別（`openid email`）で、
  BigQuery には利用者本人の認証情報で接続する(`BQ_AUTH=user_oauth`)。
- ホスト型(`auth/as_provider.py`)では Google に求めるスコープは `openid email` のみで、BigQuery には
  サーバーのサービスアカウントで接続する(`BQ_AUTH=adc`)。
- **OAuth の `sub`（subject）→ `persons.person_id` のマッピング表**を別テーブルで持つ（個人情報分離）。
  未マッピングは `usage_log.actor_raw` にハッシュで記録し、後で名寄せ。
- **許可リスト(ホスト型)**: リモート MCP サーバーとして動かすときは、サーバー自身が OAuth の認可サーバーと
  なり(`docs/SETUP_gcp.md` §8)、Google の callback で `oauth_identity_map` に無い `sub` を拒否する
  (未登録者を訪問者として通さない)。事前登録は `pending:<person_id>:<email>` の行で行い、初回ログイン時に
  `email_verified`(と、`[org] workspace_domains` のアドレスでは組織の Workspace の `hd`)を確かめて実際の
  `sub` に結び付ける。

## 6. 4D 出力規約（システムプロンプト・署名型）

`signature/prompt.py` が供給する固定文（注入する規約）：

1. ユーザ入力ごとに、必ず `log_usage` を呼ぶ（`use_category`, `data_kinds`, `summary` を生成して送る）。
   `use_category` は `l2`/`l1`/`l0`/`prohibited`（L0/L1/L2＋禁止列挙）から選ぶ。
2. 組織固有の語・人名・略語が出たら `lookup_terms` で解決してから答える。
3. 出力の冒頭に委任タグ（`[AI完結]` / `[要人間判断]`）。
4. 解釈に曖昧さがあれば「〜の意味で解釈しました」を一行添える（Description）。
5. 出力末尾に署名（Diligence/Discernment）：
   「— この回答はAIが下書きしました。数値・固有名詞は未検証です。利用者が確認・修正のうえ確定してください。」
6. 貼付データに個人情報の気配があれば、処理前に `get_pii_anonymization_guidance` を**列名のみ**で
   呼び、一旦止まって目的確認と匿名化仕様の承認を得る（下限 FLOOR-01）。
7. **読みやすさも規約のうち**。結論を先に1〜2行で／Description は複数の
   解釈が実際にありうるときだけ／status の明示は回答内で1回だけ。

規約は**証拠を残すこと**に最適化されており、そのままでは定型が本文を埋める（複数の問いをまとめた
回答に同じ「暫定です」が繰り返し出る、など）。7はその是正であり、1〜6を弱めるものではない（委任タグと署名は残す — 短く、かつ「AIが下書きした」ことの表示という
制度の本体だから）。

## 7. ポジティブリスト監視（monitor）

全員が同じ MCP/フックを入れる前提で、**台帳行が来ない＝未接続/シャドーAI**として炙り出す。
穴を塞ぐのでなく沈黙を異常として検出。

```
positive_list_check(expected_actors: set, window: 直近N日):
  seen    = SELECT DISTINCT actor_id FROM usage_log
            WHERE occurred_at >= now-window
              AND log_id NOT IN (SELECT log_id FROM usage_withdrawals)  # R6 取り下げ行を除く
  missing = expected_actors - seen
  report(missing)   # 管理者へ未接続リストを出力
```

- `expected_actors` は `persons.status='active'` から生成。
- 出力は管理コンソール or スプレッドシート（一括確認用）。

## 8. 受け入れ基準（テスト観点）

| # | 観点 | 期待 |
| --- | --- | --- |
| T1 | `lookup_terms` 正常 | 既知用語で hit を返す。定義・スコープを含む |
| T2 | ACL 遮断 | sensitive/dept 用語を権限なし閲覧者に**返さない**（WHERE句でも除外） |
| T3 | ハイブリッド | 表記揺れ・同義の質問でも VECTOR_SEARCH 経由で hit（lexical 単独より再現↑） |
| T4 | RRF融合 | lexical/semantic 両方の上位が適切に統合される |
| T5 | `log_usage` 記録 | INSERT成功、`occurred_at`/`actor`/`use_category`/`summary` が入る |
| T6 | 要約のみ | 逐語の生プロンプトが台帳に保存されない |
| T7 | OAuth | 認証成功で sub 取得、persons へマッピング、未マップは `actor_raw` |
| T8 | 監視 | active なのに台帳ゼロの人物を missing として検出 |
| T9 | 署名 | 出力に委任タグ＋末尾署名が必ず付く（システムプロンプト経由） |
| T10 | パーティション | `usage_log` が日付パーティションで作成され、期間クエリが効率的 |

各テストは `tests/test_t01..t10` に対応。テストはオフラインで、BigQuery の代わりに InMemory のストアを使って実行する。

## 9. Policy Layer（AIポリシーの配達）

> **語彙**: ポリシーの語彙は **level 0/1/2（採用レベル）＋床行（`is_floor`＝明示的禁止リスト）** である
> （`get_applicable_policy` は level2/level1/level0/floor 別＋legend で返す）。台帳の `use_category` も
> 同じ語彙（`l2|l1|l0|prohibited`）を使う。§9.4 の `suggested_category` は発火行の level/is_floor を
> この台帳語彙へ写像して返す。

組織のAIポリシーを「利用の現場に届く実行可能なデータ」にする層。人間が行を起案・承認し、
WACA Loop は**読む（配達係）だけ**——行の承認・書換えはしない（§9.6 の境界）。ブロックはせず、
警告と迂回路の提示のみ（統治の負荷はリスクに比例）。個人ポリシー（③）は対象外——
組織の床（①`scope_unit='org'`）＋部署（②`scope_unit`=部署のユニット）のみ。

### 9.1 データモデル

`<DS_FEEDBACK>.policy`（`sql/ddl/08_policy.sql` / `models.PolicyRule`）:
`row_id`（人が付ける安定キー、例 KYO-6。シートからの同期 `scripts/sync_policy_from_sheet.py` は番号を
ゼロ埋めしない形に揃える。ストアは row_id を不透明な文字列として扱い、形式は検証しない）, `scope_unit`,
`level`（採用レベル 0/1/2。床行は NULL）, `line`, `effect`
（床の迂回路 / 条件）, `is_floor`, `status`, `version`, `trigger_kinds`
（この行を発火させる data_kinds。**人が書き WACA Loop は読むだけ**）。

### 9.2 配達 — `get_applicable_policy`

閲覧者の適用行（床＋自部署）を採用レベル別（level2/level1/level0）＋床（floor）で返す
（`legend` に語彙説明を同梱。**可視行ゼロのビューアには legend も空** — フェイルクローズ）。
level 未設定の業務行は消さずに level1 として配達する（§9.4 の判定既定と一致 —
ACL を通った行が応答から黙って消えてはならない）。ACL は他ストアと同じ二重ゲート
（SQL WHERE ＋ in-process `policy_visible_to`）。実装済み。

### 9.3 台帳アンカー — `log_usage.policy_row_ids`

クライアントは `get_applicable_policy` で該当した行の `row_id` を `policy_row_ids` で渡す
（任意——渡さないクライアントの摩擦はゼロ）。台帳には行ID配列と、適用行の最大 `version` を
記録する。監査は台帳→ポリシー版を突合でき、月次の計器盤（§9.6 の列J 書き戻し・将来）の入力になる。

### 9.4 判定と警告 — `policy/classify.py`（純ロジック）

`classify_usage(data_kinds, declared_category, rules)`：`trigger_kinds ∩ data_kinds` が非空の行を
発火とみなし、発火行の level/is_floor を台帳語彙へ写像（**下限→prohibited**、level0→l0、
level1→l1、level2→l2、level 未設定の業務行→l1＝§9.2 の配達既定と同じ保守側）した最重値を
`suggested_category` に。**下限は level0 と別バケット**（最重の `prohibited`）である。発火行のうち `is_floor` は
`floor_warnings: [{row_id, line, detour}]` として迂回路つきで返す。`mismatch` は申告区分が
サジェストより軽い場合 true。**助言のみ**——記録は常に成功し、台帳の `use_category` は
申告値のまま（人の判断を尊重・責めない）。rules は必ず二重ゲート通過後のものを渡す
（他部署の trigger が漏れない）。

`data_kinds` 追加語彙: `core_asset`（コア資産の丸ごと持ち出し相当）/ `final_judgment`
（最終判断の委任相当）。床行の trigger は行の `trigger_kinds` に持つ（例:
FLOOR-01=`pii` / FLOOR-02=`core_asset` / FLOOR-03=`final_judgment`。`sample/policy.json` の記入例には
FLOOR-01・FLOOR-02 の2行がある）。

### 9.5 受け入れ基準

| # | 観点 | 期待 |
| --- | --- | --- |
| P1 | 台帳アンカー | `policy_row_ids` 指定で行ID配列＋version が台帳に入る。未指定は空配列 `[]` |
| P2 | 床警告（is_floor） | `data_kinds=["pii"]` で床行の `floor_warnings`（迂回路つき）が返り、記録は成功する |
| P3 | 不一致フラグ | l2 申告＋pii → `mismatch=true`。台帳の `use_category` は申告値のまま |
| P4 | 非発火 | trigger に触れない data_kinds では警告なし・mismatch=false |
| P5 | ACL | 他部署スコープの行は判定に参加しない（二重ゲート） |
| P6 | level→台帳語彙の写像 | level0→`l0`（下限でなくても）/ level2→`l2` / level 未設定→`l1` |
| P7 | `prohibited` 申告 | 下限発火時に `prohibited` を申告すれば mismatch=false、迂回路は出る |

各テストは `tests/test_policy_anchor_classify.py` に対応。未実装の将来分：役割（ポリシーの承認者・
管理者・推進者・部署の責任者を persons・units に対応づける）・ポリシー改訂タスク（改善のループの
`incident_flag` などから改訂タスクを提案する）・計器盤の列J書き戻し（§9.6）。

### 9.6 シートとしてのポリシー — 配達係・計器盤・境界

ポリシーは **行IDを持つシート**として運用する（IDの例: KYO-6 / SEC-3）。人が書く A〜I 列は部署の責任者・
承認者が起案し、J 列は WACA Loop が書く利用の計器である。WACA Loop の関与は次の **2方向のみ**とする。

- **配達係（A〜I 列を読む）**: 人が書いたポリシーの行を WACA Loop に同期する
  （`scripts/sync_policy_from_sheet.py`）。`get_applicable_policy` が利用の現場で該当する行を返す。
- **計器盤（J 列のみに書き戻す）**: `log_usage` にポリシーの **行ID** を記録する（§9.3、実装済み）。
  行ごとの件数を **J 列** に書き戻すこと（使われていない行は、不要な行かシャドーAIの兆候）と、
  質問の記録から **該当する行がない** 場面を抽出して新しい行の候補とし、他部署で承認済みの類似の行を
  横展開として提示することは **未実装**（将来の拡張）。
- **境界（不変条件）**: WACA Loop がポリシーのシートに書くのは **J 列のみ**である。行を承認しない、
  人が書く A〜I 列を書き換えない、見直しの判定を代行しない。WACA Loop が止まっても紙で運用できる。
  現在の実装はシートを読むだけで、書き込みを一切行わない。J 列への書き戻しを実装する場合は、
  書き込み範囲を J 列に限定する形で機械的に強制する。

行の配達ステータスはシートの「承認日／発効日」から機械的に決める（未記入=`trial`／記入済=`approved`）。
WACA Loop は承認日を書かない。

### 9.7 名簿・ディレクトリに関する一線の判定基準

「名簿かどうか」の二択ではなく、**3つの軸**で床（禁止の一線）を判定する。

- **経路**: 安全チャネル（WACA Loop＝学習不使用＋守秘＋アクセス制御）の中であれば床ではない。安全で
  ない外部ツールや外部への持ち出しは床に当たる。
- **粒度**: 目的に必要な最小限（氏名・所属・役割・専門・同意）は床ではない。生の個人情報の束（生年月日・個人の
  連絡先・ID）は床に当たる（安全チャネル内でも sensitivity=sensitive＋owner_unit＋steward 限定で最小限とする）。
- **全量・復元性**: 目的の範囲の一部であれば可。丸ごと、あるいは全件を復元できる形は床に当たる。

設計上の裏付け: `persons` のスキーマは個人情報を最小限にしている（display_name・unit_path・consent・status
のみで、生年月日や連絡先の列を持たない）。ガードレールとして、persons に生の個人情報の列を足さないこと、
人事の名簿をそのまま入れないこと。同意の既定はオプトアウト（組織内の探索＝true、組織外＝false）とする。

## 付録 不変条件の番号（INV-*）

コードとテストのコメントは、次の番号で不変条件を参照する。

| 番号 | 不変条件 |
|---|---|
| INV-1 | 逐語を持たない（台帳・記録は要約のみ。T6） |
| INV-2 | 純ロジックは標準ライブラリのみに依存する（BigQuery・OAuth・MCP は関数内で遅延 import） |
| INV-3 | データのストアは InMemory と BigQuery の対で、同じ面を持つ |
| INV-4 | 固定列を増やさず、拡張は `attributes` JSON に入れる |
| INV-5 | 評価ではなくマッチング（人に順位・点数を付けない） |
| INV-6 | 警告のみ（利用者の作業をブロックしない） |
| INV-7 | 同意の分離（組織内の同意と組織外の同意を分け、ERN は組織内の同意のみを見る） |
