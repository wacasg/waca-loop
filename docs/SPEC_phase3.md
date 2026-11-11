# WACA Loop — 実装仕様書(ERN（専門度・貢献量・信頼度エンジン ＋ find_expert）)

正典。`docs/SPEC_phase1.md` / `docs/SPEC_phase2.md` と同じ規約で、コードは `§X` で本書を参照する。
実装と本書に差分があれば、本書側を直すか、差分の理由を §7 に残す。

---

## 0. 位置づけ

- **範囲**: 「つなぐ」機能の ERN 本体。3エンジン（① 専門度 ② 貢献量
  ③ 信頼度）と、それを唯一の出口とする MCP ツール `find_expert`。
- **前提**: `ServerContext._clamp_k`（FLOOR-02、`server.py`）。`find_expert` は
  この関数に `tool="find_expert"` で乗る。新しい上限機構を作らない。
- **原則**:
  - 専門家候補の提示は**上限3名・順不同・質的根拠のみ**（数値・順位・星を出さない）。
  - **非対称開示**: 候補者本人には通知しない（完全サイレント。監査証跡のみ残す）。
  - ルーティングの対象は組織のメンバー（`is_org_member`）に限る。
  - 鮮度は半減期180日の指数減衰を既定とする（**設定値必須**。係数はデータ蓄積後に調整する）。
  - コールドスタート（シグナルゼロ）は役職ベースのディレクトリにフォールバックし、根拠種別を出力に明示する。
  - ERN・台帳を人事評価の減点に使わない。
- **根拠**: 不変条件 INV-1 / INV-5 / INV-7（SPEC_phase1 付録）、§3.3。
- **非目的（ERN が行わないこと）**:
  - 人の順位付け・スコア表示・星・件数の開示。ERN は **評価ではなくマッチング**。
  - graph 方式ルーティング。最小構成はシグナル集計＋役職フォールバック。
  - 本人通知（完全サイレント）。
  - `consent_external`（外部開示）の利用。ERN は `consent_internal` のみを見る。
  - circles の配線（§4d）。
  - 重み係数のチューニング（データ蓄積後・§7）。

---

## 1. データセット

| テーブル | データセット | 種別 |
|---|---|---|
| `ern_assertions` | `<DS_KNOWLEDGE>` | 人 × 領域の宣言／導出プロファイル |
| `ern_audit` | `<DS_FEEDBACK>` | append-only 監査証跡（誰が・いつ・何を探したか） |

DDL は `sql/ddl/15_ern.sql`。`scripts/init_bigquery.py` が `<DS_KNOWLEDGE>` / `<DS_FEEDBACK>` を置換する。
**シグナルそのものは新テーブルを作らない** — 専門度・貢献量は `<DS_KNOWLEDGE>.feedback` の
既存行（`fb_type` × `created_at` × `author_id`）からの**導出値**である。増やすのは
「宣言」と「監査」だけ。

---

## 2. データモデル

### 2.1 `ErnAssertion`

```
assertion_id      STRING NOT NULL
person_id         STRING NOT NULL        -- <DS_GROUNDING>.persons.person_id
topic             STRING NOT NULL        -- 領域ラベル（正規化済み）
embedding         ARRAY<FLOAT64>         -- リコール専用。スコアには一切寄与しない（§2.3注）
source_candidate_id STRING               -- 由来（extraction_candidates 等）
status            STRING NOT NULL        -- declared | derived | frozen
created_at        TIMESTAMP NOT NULL
updated_at        TIMESTAMP NOT NULL
```

- `rank` / `score` / `rating` / `order` 列は**置かない**（E3）。順位・総合点はテーブルに残さない。
- `embedding` は「その領域に近い問いを引く」ためだけに使う。ベクトル距離をスコアに足さない
  (no vectors for scoring)。

### 2.2 `Feedback.created_at`

鮮度減衰は `feedback.created_at` に依存する（`sql/ddl/09_sfaq.sql`）。`Feedback` dataclass と
InMemory / BigQuery 両ストアの行マッパーはこの列を持つ（ストアペア規約）。ERN 本体はこの列を鮮度減衰の入力に使う。

### 2.3 スコア算出式（3エンジン）

**① 専門度** — person × topic ごとに、その topic に紐づく問いへのフィードバックを集計する。

```
expertise(p, T) = Σ_e [ w(e.fb_type) · decay(age_days(e)) ]  −  Σ_r [ w_refuted · decay(age_days(r)) ]
decay(t)        = 0.5 ^ (t / half_life_days)
```

初期重み（`ern/weights.py` の `WEIGHTS` dict 1箇所に集約。値は Open item・§7）:

| シグナル | `fb_type` | 重み |
|---|---|---|
| 採用された判断 | `decision` | `w_adopt = 3.0` |
| 訂正・補足が採られた | `supplement` | `w_correct = 1.5` |
| 参照された | `useful` | `w_ref = 1.0` |
| 同意された | `agree` | `w_agree = 0.5` |
| 反証された（減算） | `refute` | `w_refuted = 2.0` |

`half_life_days` は **config 由来**（E9）。既定 **180日**（ロジック内リテラル禁止）。

**② 貢献量** — 単純カウント（減衰なし）。`fb_type` を問わず、その人が書いた行数。
コールドスタート判定（シグナルゼロか否か）にのみ使い、出力しない。

**③ 信頼度** — `ern_audit` の append-only 証跡そのもの。「いつ・誰が・何を根拠に候補になったか」を
後から再現できることを信頼度と定義する。数値化しない。

> **注**: ①②③のいずれも **外部に数値としては出ない**。出るのは §3 の band（帯）と evidence_type だけ。

---

## 3. MCP ツール `find_expert`

- 発火は**質問者起点のみ**（誰かを常時ランキングする経路を作らない）。
- 上限 **3名**（E2）。`server.py` の `_K_CAPS["find_expert"] = 3` と
  `self._clamp_k(k, "find_expert")` で締める。**break-glass の対象外**（`persons` と同じ扱い）。
- 並びは **person_id 昇順に固定ソート**して内部選定順を破壊する（E2）。「良い順」を構造的に作らない。

戻り値（許可リスト。これ以外のキーを足さない — E1 の共通ヘルパ `ern/schema.py:assert_no_scores`
で全 ERN ツールに適用）:

```json
{"candidates": [
  {"person_id": "...", "display_name": "...", "units": ["..."],
   "topic_match": "近い|やや近い",          // band。数値でない
   "freshness": "最近|しばらく前",           // band。数値でない
   "declared_topics": ["..."],
   "evidence_type": "実績ベース|役職ベース"} // E10
]}
```

**返さないもの**: スコア生値、件数、根拠 `question_id`、フィードバック本文、順位、
「誰が探したか」（E6）。

---

## 4. 漏洩防止・ACLゲート（最重要）

**a. mask_author 該当行はルーティング原料から除外。**
`provenance = 'corporate_training' ∧ perm_corporate ∧ ¬perm_individual` の問いに付いた
フィードバックは、著者を伏せる約束（SPEC_phase2 §2.3 E-3(ii)）で配信されている。これを
専門度の材料にすると、伏せたはずの著者が「その領域の専門家」として名前で出る。**集計前に落とす。**

**b. Gate1 — プロファイル元の質問集合は質問者の可視範囲を継承。**
専門度の集計対象は、**質問者（viewer）の** `acl_where_clause` / `visible_to` を通った問いに限る。
ERN 独自の可視範囲を持たない。§3.3 の二重ゲート（SQL WHERE ＋ in-process 再フィルタ）を
そのまま踏襲する。

**c. Gate2 — 開示ゲート。**
候補として名前を出してよいのは以下を**すべて**満たす人物のみ:

```
viewer.is_org_member ∧ person.consent_internal ∧ person.status == 'active'
```

非 org メンバーは**常に空**（fail closed）。`consent_internal=false` は最強シグナルでも出ない
（E4）。判定は in-process（`person_visible_to` 相当）と BigQuery 側 WHERE 句の**両方**に
書き、WHERE 句文字列に consent 条件が含まれることをテストで表明する（E4(b)）。
キャッシュを置かない — opt-out は次の呼び出しで即時に効く（E5）。

**d. circles は常に3軸ACLに AND 劣後。**
ERN では circles を配線しない。ERN モジュールは `circles` を import しない（E12(a)）。
将来配線しても、サークル共有を理由に可視範囲を**広げない**。

**e. スコープ判定は `retrieval/acl.py` 経由のみ。**
ERN 新規モジュールに `"personal"` / `"dept"` / `"org"` / `"S0".."S5"` のような段階名 / 三色語彙の
文字列比較を直書きしない（E11・grep ゲート）。

**f. 台帳が開示経路にならない。**
`find_expert` は計器盤に載る（E12(b)）が、その記録に**候補者名・person_id を含めない**（E13）。
含めると「誰が誰を探したか」が台帳から読め、E6 の非対称開示が台帳経由で破れる。
「誰が探したか」は `ern_audit` にのみ残り、これを読み出す MCP ツールは作らない。

> **どちらの台帳か。** `find_expert` の記録は `_trail()` ＝ **`tool_events`**（R3a 計器盤）に載せ、
> `UsageRecord`（`usage_log`）には書かない。理由: `usage_log` は
> `review_pending_usage` / `list_withdrawn_usage` というスチュワード向けの読み出し経路を
> 持つのに対し、`tool_events` にはそれが無い。「誰が誰を探したか」を遠ざけるという §4f の
> 目的からは、**読み出し経路の少ないほうに載せるのが正しい**。また `log_usage` は
> 利用者（クライアント）起点で呼ぶ設計で、サーバが勝手に書く器ではない。
>
> `ACCESS_MATRIX.md` §2.2 が「台帳」を `usage_log` の意味で使っているので、
> ERN について「台帳」と書くときは**計器盤（`tool_events`）**を指す。

---

## 5. ライフサイクル

- `persons.status = 'frozen'` になった人物の assertion は `status='frozen'` へ。**1年後に delete**。
- purge の起点は `persons.updated_at` の代理（**弱点**: 別要因の更新で時計が延びる。
  専用の `frozen_at` を持たない判断はコスト優先の暫定で、恒久化するなら列追加が要る）。
- `scripts/purge_frozen_ern.py --dry-run` を用意。既定 dry-run、実削除は明示フラグ。

---

## 6. 受け入れ基準

`docs/ERN_ACCEPTANCE_TESTS.md` の E1〜E13 をそのまま採用する。本書との対応:

| E# | 観点 | 本書 | 不変条件 |
|---|---|---|---|
| E1 | 出力に数値評価が無い | §3 許可リスト | INV-5 |
| E2 | 上限3名・順不同 | §3 | INV-5 |
| E3 | DDL に rank/score 列が無い | §2.1 | INV-5 |
| E4 | 未同意者は絶対に出ない（二重ゲート） | §4c | INV-7 |
| E5 | オプトアウト即時反映 | §4c | INV-7 |
| E6 | サイレント非対称開示 | §3・§4f | INV-7 |
| E7 | シグナルは append-only | §1・§2.3 | — |
| E8 | 逐語なし（4点検査） | §1・§2.1 | INV-1 |
| E9 | 半減期は config 由来 | §2.3 | — |
| E10 | 根拠種別の明示 | §3 `evidence_type` | — |
| E11 | スコープ判定は acl.py 経由のみ | §4e | §3.3 |
| E12 | circles 非依存・計器盤搭載 | §4d・§4f | — |
| E13 | 台帳が開示経路にならない | §4f | INV-7 |

テストは `tests/test_e01_*.py` 〜 1ファイル1観点、InMemory フィクスチャでオフライン実行。

テストファイル:

| E# | ファイル |
|---|---|
| E1 | `tests/test_e01_no_numeric_scores.py` |
| E2 | `tests/test_e02_limit_and_order.py` |
| E3 | `tests/test_e03_no_score_columns.py` |
| E4 (a)(b) | `tests/test_e04_consent_double_gate.py` |
| §4a 著者マスク | `tests/test_e04b_masked_authors_never_route.py` |
| E5 | `tests/test_e05_optout_is_immediate.py` |
| E6 | `tests/test_e06_silent_asymmetry.py` |
| E7 | `tests/test_e07_append_only.py` |
| E8 | `tests/test_e08_no_verbatim.py` |
| E9 | `tests/test_e09_half_life_from_config.py` |
| E10 | `tests/test_e10_evidence_type.py` |
| E11・E12(a) | `tests/test_e11_scope_via_acl_only.py` |
| E12(b)・E13 | `tests/test_e12_e13_ledger_is_not_a_disclosure_path.py` |
| §5 ライフサイクル | `tests/test_ern_lifecycle.py` |

フィクスチャは `tests/conftest.py` の `ern_context` / `ern_persons` / `ern_viewer` /
`ern_seed_fn`。名簿は**同意あり2名 ／ オプトアウト1名 ／ 退職者1名**を必ず置く —
「最強シグナルでも出ない」（E4）を、シグナルが弱いせいで通る検査にしないため。

---


### 不変条件との対応

| 不変条件 | 対応する E# | 本書 §|
|---|---|---|
| INV-1 逐語を持たない | E8 | §1・§2.1 |
| INV-5 評価ではなくマッチング | E1・E2・E3 | §0・§2.1・§3 |
| INV-7 同意分離 | E4・E5・E6 | §4c |
| §3.3 ACL二重ゲート | E4(a)(b)・E11 | §4 |

## 7. 未実装・保留（Open items）

unit × データ種別の可視性は `docs/ACCESS_MATRIX.md` に定める。`persons` と `find_expert` は
**break-glass の対象外**である（ACCESS_MATRIX §1）。

- **重み係数**（`WEIGHTS`・`half_life_days`）— データ蓄積後に調整する。
  実装済みの置き場所は `ern/weights.py` の1箇所と `config.ern_half_life_days`
  （既定 180 日）。ロジック内リテラルは無い（E9・AST で表明）。
- **band の境目**（`TOPIC_MATCH_STRONG` / `FRESHNESS_RECENT_DAYS`）— 重み係数と同じ性質の
  未調整値。`ern/weights.py` に集約してある。
- **graph 方式ルーティング** — 将来の拡張。
- **本人通知**（月次サマリ）— 将来の拡張。現在は完全サイレント（E6 で表明）。
- **`consent_external`** — ERN では未使用。
- **組織が著者の判断**。`author_id=None` の判断は ERN のシグナルにならない（`signals.aggregate` が落とす）。
- **人事利用の線引き** — 減点利用は禁止、加点考課への利用は可。ツールの表示制約
  （§3）はこの線引きと独立に常に効く。**ツールの description には「この結果で人を評価・
  査定してはならない」と書いてある**が、それは規約であって仕組みではない。
- **`frozen_at` 列**の要否（§5 の弱点）。`ern/lifecycle.py` は
  `persons.updated_at` を代理に使っており、**別要因の更新で時計が延びる**。
  `overdue` は「退職から1年」ではなく「その代理で1年」である。
- **リコールの粒度**。`engine.question_matches_topic` は `attributes.domain` の完全一致と
  問い本文の部分一致だけ。領域ラベルの正規化・同義語の扱いは未着手。
- **宣言（`ern_assertions`）は候補を生まない**。
  `_ern_people` は `persons_for_topic` の id を集めるが、`build_candidates` が選ぶのは
  シグナル由来か役職フォールバックだけで、**宣言だけの人は候補にならない**。宣言は
  「すでに選ばれた人に `declared_topics` を添える」役にしか使われていない。
  fail-closed 側なので漏れではないが、`embedding`（リコール専用・§2.1）には
  **今のところ消費者が無い**。宣言を候補源にするかは未決である — するなら、
  §4a で落とした著者が宣言経由で名前として出ないことを別途確かめる必要がある。
- **役職フォールバック（`ern_role_fallback`）は未配線**。
  `SharedRuntime` にフィールドはあるが `build_shared_runtime` が設定しておらず、
  config にも供給元が無い。値を入れているのはテストだけなので、**シグナルゼロの領域では
  本番の `find_expert` は黙って0件を返す** ＝ E10 のコールドスタートは配線するまで効かない。
  配線するときの制約: **著者情報から導出してはならない**。組織図・役職から人手で作ること。
  導出すると、§4a で落としたはずの著者マスク該当者が「役職ベース」として名前で出る経路に
  なる。
- **`ern_audit` のスコープ削除**。§5 の purge で `ern_assertions` から人を消しても、その
  `person_id` は他人の `candidate_ids` に、パーティションの期限（365 日）が来るまで残り、
  **削除が完結しない**。purge で `candidate_ids` を洗浄する処理が要る。
- **`topic` の統制語彙への解決**。いまは「24 字以内・句読点なし」という形の検査だけで
  自由文を弾いている（用語の最長がこれに収まることが根拠）。24 字以内で句読点の無い文は書けるので、
  **意図的な迂回は止まらない**。本筋は領域ラベルを統制語彙へ解決して、解決できたものだけを
  残すこと。
