# WACA Loop — 実装仕様書(SFAQ・判断ナレッジ層)

正典。SFAQ(判断ナレッジ層)の実装仕様である。基本の正典は [SPEC_phase1.md](SPEC_phase1.md)。

## 0. 位置づけ

SFAQ(Semantic Feedback Analytics for **Questioning**、頭字語読み・「スファック」とは読まない)は
問い駆動の判断ナレッジ層。中心は「問い」で、そこに6型のフィードバックが積もる。
内側ループは 仮説→行動→結果→気づき の順で回り、問いは外側ループの境界に置く。

## 1. データセット

`<DS_KNOWLEDGE>`(既定 `waca_loop_knowledge`、`DS_KNOWLEDGE` で可変)に
`questions` / `feedback` の2テーブル([sql/ddl/09_sfaq.sql](../sql/ddl/09_sfaq.sql))。

## 2. データモデル

### 2.1 questions / feedback

**埋め込み対象は「本文＋見出し」**である。`question_text` だけを対象にすると、検索の当たり外れが
見出しを作る**規則の出来に依存する**(規則が空振りした行が引けない)。学びの本文
(`feedback.body` の `fb_type='decision'`)がデータ本体、`question_text` はその見出しであり、
埋め込みは**両方**を対象にする。見出しは派生物なので、規則を改良して作り直せばよく、
コーパスの再埋め込みは要らない。実装は
[`knowledge/embed_text.py`](../src/waca_loop/knowledge/embed_text.py)(純関数)と、
同じ連結を SQL で組む `scripts/seed_sfaq.py --backfill-embeddings`(双子)。片方を変えたら
もう片方も変えること。**クエリ側は問い合わせ文をそのまま埋め込む**。

**`questions.owner_unit`** — スコープ3軸ACL(§3.3)の
personal/dept ゲートに必須(terms.owner_unit と同じ役割。personal 行の owner_unit は
本人だけが属する unit)。`feedback.author_id` は E-3(ii) 該当時に配信時マスクする。

### 2.2 6型フィードバック(S5)

`decision|agree|refute|supplement|open|useful`。**AI が分類して付与し、人はラベルしない**。
MCP 経路では呼び出し側の AI が fb_type を判断して渡す(enum 検証はサーバ)。

### 2.3 公開判定(E-3・最重要)

本書とコードでは、この許諾の判定を **E-3**(許諾ゲート)、下表2行目の「法人許可あり・個人許可なし」の
扱いを **E-3(ii)** と呼ぶ(ERN の受け入れ基準 E1〜E13 とは別の記号)。

スコープ3軸 × provenance×perm の **AND 評価**。二重ゲート(SQL WHERE + in-process)は
[knowledge/acl.py](../src/waca_loop/knowledge/acl.py) の
`question_where_clause` / `question_visible_to` が同一規則で実装する。

| provenance | 法人許可 | 個人許可 | 結果 |
|---|---|---|---|
| corporate_training | true | true | 公開(法人スコープ内) |
| corporate_training | true | false | 法人内のみ・**author を伏せて**共有(E-3(ii) → `mask_author`) |
| corporate_training | false/None | — | 非公開 |
| individual | — | true | 公開 |
| individual / other | — | false | 非公開(fail closed) |

### 2.4 状態機械と配信

`candidate → provisional → approved`(+ `contested`)。生ログ→candidate は自動、
**承認は人(スチュワード)**。配信対象は provisional/approved/contested
(既定では暫定も**status 明示つき**で出す。candidate は未整形のため配信しない)。
状態遷移ツールは `steward` ユニット限定。

## 3. MCP ツール(read/write 分離)

| tool | 種別 | 挙動 |
|---|---|---|
| `search_sfaq(query, k)` | read | ハイブリッド検索(lexical+VECTOR_SEARCH+RRF)→ 問い+全フィードバック。二重ゲート・E-3(ii) マスク・status 明示 |
| `add_sfaq_feedback(question_id, fb_type, body)` | write | フィードバックB(復唱→確認→感謝)の記録先。可視な問いにのみ書ける |
| `add_sfaq_question(question_text, first_feedback?, owner_unit?)` | write | candidate 起票。provenance=individual・perm_individual=true(起票=本人合意)。スコープは **dept/internal/normal・所有ユニット=起票者の部門**(§3.3)。全社可視(org)を既定にしない — 起票された問いは一個人の未査読の記述であり、org 既定は着地と同時に組織全体へ開く。複数部門に所属する起票者は `owner_unit` の明示が必須(推測しない・所属外の指定は拒否)、所属ユニットが無ければ起票自体を拒否する(fail closed) |
| `set_sfaq_status(question_id, status)` | write | スチュワード専用の状態遷移 |

システムプロンプト(§6)に「判断・方針・経緯は search_sfaq を参照、status を回答に明示、
価値ある判断は復唱→確認→感謝で記録」を含める。

## 4. 投入経路

- **seed(整形済みのデータ)**: `scripts/seed_sfaq.py <seed.json>` → `--backfill-embeddings`。
- **過去の事例データを一括投入する経路**(未実装): 1資料複数問い→複数レコード(S1)、
  投入時の既定 personal+perm_individual=false(S2)、status=provisional(S4)。

## 5. 未実装

- 一括投入(S1)とフィードバックC(標本→全数是正、S7)— 一括投入した分の承認に使用
- 重複統合の提示(auto_merge 列・既定 false は実装済み、クラスタリング提示は未実装)
- サークル CRUD(persons.circles のデータ構造まで)

## 6. 受け入れ基準

| # | 観点 | 状態 |
|---|---|---|
| S1 | 一括移行・分割 | 未実装 |
| S2 | 移行既定=非公開 | ✅ tests/test_sfaq.py(seed 既定も fail-closed) |
| S3 | 公開判定(ii) | ✅ 法人内のみ・author マスク |
| S4 | 暫定ステータス | ✅ seed 既定 provisional・status 明示配信 |
| S5 | FB自動分類 | ✅ 6型 enum 検証・呼び出しAIが付与 |
| S6 | 自動統合 | ✅ auto_merge 既定 false |
| S7 | フィードバックC | 未実装 |
| S8 | 掲載適格 | 部分(read/write 分離済み) |
| S9 | Vertex埋め込み | ✅ questions.embedding + VECTOR_SEARCH(bq_ml) |
