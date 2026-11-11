# ACCESS_MATRIX — unit × データ種別の可視性

本書は、break-glass の保有者（§1）と、データ種別ごとの ACL 3軸 ＋ `owner_unit` の既定（§2）を定める。

**この文書は組織としての既定の一覧である。** 可視性は個々のレコードごとにも決まるが、種別としての
既定はここに書く。ERN の `find_expert`（人を返すツール）はこの既定に従う（SPEC_phase3 §7）。

> **導入形態**: この文書の保証は、BigQuery に台帳とデータを置く形態のものである（提供している
> 形態はこれだけ。§2.3）。
>
> **実装との関係**: この表は `src/waca_loop/retrieval/acl.py` ほかの**実装を記述したもの**で
> あって、実装を上書きするものではない。食い違いを見つけたら、直すのはコードか本書のどちらか
> — **黙ってどちらかに合わせない。**

---

## 0. 先に読む3つの落とし穴

この3つを知らずに表を読むと、書いてあることを取り違える。

### 落とし穴1 — `personal` / `team` / `dept` は、実装では**同じ1つの規則**に潰れる

`scope_tier` は4値（`personal` / `team` / `dept` / `org`）だが、`retrieval/acl.py:_tier_ok` は
org 以外をすべて「**viewer が record の `owner_unit` に居るか**」に帰着させる:

```python
if tier == ScopeTier.ORG:
    return True
if owner_unit is None:
    return False
return owner_unit in viewer.units
```

DDL に per-person の所有者列が無いので、`personal` を「本人だけ」に絞る材料が無い。
**`personal` と書いても、実際に見えるのはその `owner_unit` に属する全員である。**
`secretariat` を owner にした `personal` の行は、`secretariat` の全員に見える。

→ **表の tier 欄は「意図」ではなく「実際の到達範囲」で読むこと。**

### 落とし穴2 — `org` を owner にすると、スコープが付いているのに全社公開になる

`ORG_WIDE_UNITS = {org.toml の [units] org}`（既定 `org`）で、`org` は
`scripts/sync_persons_from_sheet.py` が**全員の `unit_path` に足している**。
したがって `owner_unit='org'` の `dept` 行は、`owner_unit in viewer.units` が全員で真になり、
**`org` tier と同じ範囲に届く** — 見た目は絞られているのに。

`ORG_WIDE_UNITS` を参照しているのは **`owning_units` → `resolve_owner_unit`（書き手側）の
1箇所だけ**である（`person_visible_to` / `person_where_clause` は `units` に一切触れていない —
名簿のゲートは同意・在籍・メンバー資格で決まり、unit 照合をしない）。

つまり**読む側では罠が生きている**。レコードの読みゲート（`visible_to` /
`acl_where_clause`）が `ORG_WIDE_UNITS` を参照しないのは意図的で、絞りたいなら tier で絞れ、
という設計である。

→ **org 相当を指したいときは `tier=org` と書く。`owner_unit=org` で絞ったつもりにならない。**

### 落とし穴3 — org tier の `owner_unit` は権限ではなく「保守者の記録」

`_tier_ok` は org tier のとき `owner_unit` を**見ない**。たとえば
`owner_unit='kyozai'` かつ org tier の用語は、全社が読める。
`owner_unit` を柵として読むと間違える。**狭めたいなら tier を変える。**

---

## 1. break-glass の保有者

### 決定

`config.export_admin_units` = **`export_admin`**（org.toml の `[units] export_admin`）。

未設定（空）なら**誰も超過できない** fail-closed になる（`config.py`）。

この unit は組織シートに無く、`sync_persons_from_sheet.manual_units` の
carry-forward で再同期を生き延びる扱い（`steward` / `owner` と同じ）。**消さないこと。**

### break-glass が効く条件（3つすべて。`server.py:_clamp_k`）

1. viewer が `export_admin_units` のいずれかに属する
2. **理由が空でない**（`reason` 引数）
3. **監査 sink が生きている**（`tool_event_store` が None でない）
   — 記録が残せないなら超過も許さない。書き込みに失敗しても普段の上限へ落とす。

超過にも天井がある: `min(k, cap * _K_OVERRIDE_MULTIPLIER)`。無制限にはならない。
超過1回につき `k_override` 監査行が1行、**同期的に**残る（他の台帳書き込みと違い、
失敗を握り潰さない）。

### 1回の呼び出しで返せる件数（`server.py` の `_K_CAPS`）

**既定値。変更は導入組織の判断で行い、黙って上げない。**

| tool | 上限 | break-glass |
|---|---|---|
| `lookup_terms` | 20 | 可 |
| `search_sfaq` | 20 | 可 |
| `sfaq_feedback` | 20 | 可 |
| `improvement_tasks` | 50 | 可 |
| `improvement_requests` | 50 | 可 |
| **`persons`（名簿）** | **10** | **不可** |
| **`find_expert`（ERN）** | **3** | **不可** |
| 既定（表に無いもの。`withdrawn_usage` 等） | 10 | 可 |

### 名簿と `find_expert` を break-glass の対象にするか — **対象外**

`export_admin` による超過は、**名簿と `find_expert` 以外の上限のためのもの**である。

根拠は2つ、いずれも独立している:

1. **コードが二重に締めている。** `lookup_terms` は
   `person_k = min(self._clamp_k(k, "persons"), _K_CAPS["persons"])`（`ServerContext.lookup_terms`）と
   書いてあり、コメントは「**語彙を持ち出す理由が、名簿のダンプまで許可してはならない**」。
   対象にするならこの二重クランプを外す改修が要る。
2. **ERN の正典が同じ扱いを明記している。** `SPEC_phase3.md` §3 は
   `find_expert` を「上限3名・**break-glass の対象外**（`persons` と同じ扱い）」と書いている。
   名簿を対象にすると、ここが正典と食い違う。

**構造で締めてある。** 呼び出し側が `reason` を渡さないことに頼るのではなく、
`_clamp_k` の中の `_NO_BREAK_GLASS = {"persons", "find_expert"}` が、保有 unit の閲覧者が
理由を書いても・監査 sink が生きていても、平常時上限を超えさせない。

呼び出し側の書き方（`reason` を渡すかどうか）に頼ると、他のツールと同じ流儀で `reason` を通す
改修が入った瞬間に **3 → 30 が黙って開く**（`_K_OVERRIDE_MULTIPLIER=10`）。30 名をエンジンが並べれば、
帯が何であれそれは順位付けで、専門家の提示は上限3名・順不同・質的根拠のみとする ERN の原則
（SPEC_phase3 §3）に反する。そのため構造で締めている。

---

## 2. unit × データ種別の可視性

### 2.1 既定の一覧

| 種別 | 実体 | 3軸を持つか | **既定** tier / audience / sensitivity | `owner_unit` の既定 | 追加ゲート |
|---|---|---|---|---|---|
| **用語** | `<DS_GROUNDING>.terms` | ✓ | `org` / `internal` / `normal` | なし（`NULL`）。教材由来のみ `kyozai` | `status='approved'` の行だけ検索に出る |
| **名簿** | `<DS_GROUNDING>.persons` | **✗** | （3軸を持たない） | — | `is_org_member` ∧ `status='active'` ∧ (`consent_internal` ∨ 本人) |
| **台帳** | `<DS_LEDGER>.usage_log` | **✗** | （3軸を持たない） | — | 本人の行のみ（`_owns`）。横断は `steward` 限定ツール |
| **SFAQ** | `<DS_KNOWLEDGE>.questions` / `.feedback` | ✓ | **`dept`** / `internal` / `normal`(`add_sfaq_question` の既定。スチュワードが `set_sfaq_scope` で広げる) | 起票した実務単位（`kyozai` / `secretariat` 等） | E-3 許諾ゲート ∧ `status ∈ {provisional, approved, contested}` |
| **改善要望** | `<DS_FEEDBACK>.improvement_requests` | ✓ | **`dept`** / `internal` / `normal` | `resolve_owner_unit` で確定（推測しない） | `steward` / `owner` は横断可。`unverified` は本人以外に出ない |
| **ERN 宣言** | `<DS_KNOWLEDGE>.ern_assertions` | **✗** | （3軸を持たない） | — | `persons` の同意ゲートに**従属**（下記） |
| **ERN 監査** | `<DS_FEEDBACK>.ern_audit` | **✗** | （3軸を持たない） | — | **読み出す MCP ツールを作らない**（下記） |

**sensitivity の既定はすべて `normal`。** `sensitive` は
`owner_unit` に居るか明示付与（`allowed_sensitive_units`）が要る**追加の締め**であって、
既定ではない。

**audience の既定はすべて `internal`。** 例外は下の 2.3。

### 2.2 種別ごとの補足

#### 用語（`terms`）

- **全行が org tier を既定とする。** 語彙は全社が引く層、という扱い。
- 教材などの保守者が決まっている語は `owner_unit` を持つが、**保守者の記録**で柵ではない（落とし穴3）。
- **`audience=public` の行は組織外の閲覧者にも届く。** `_audience_ok` の `PUBLIC` は
  `viewer.is_org_member` を見ずに真を返す。用語という種別で意図的に外向きにしてよい行は
  限られるはずなので、`public` を付けるときは中身を点検すること。

#### 名簿（`persons`）

3軸を持たない。規則:

> 組織メンバーは、同意している在籍者を**組織横断で**引ける。

- 非メンバーは**自分の行すら**返らない（`person_where_clause` が `FALSE`）。
- `status='frozen'`（退職者）は誰にも返らない。
- オプトアウトは本人以外に効く（本人だけは自分を見られる）。
- **「誰が見えるか」ではなく「1回に何人出せるか」で締めている** — それが
  `_K_CAPS["persons"]=10` である（大きな `k` で名簿を列挙させないため）。ここを ACL 側で絞るのは
  筋が悪い、と `acl.py` が明記している。
- **`consent_external` は ERN では使わない**（SPEC_phase3 §0）。

#### 台帳（`usage_log`）

3軸を持たない。**本人の行だけ**が本人に見える（`_owns`: `actor_id` / `actor_raw` /
`subject_hash` の一致）。横断して読めるのは `steward` 限定のツール
（`review_pending_usage` / `list_withdrawn_usage`）。

- `list_withdrawn_usage` は `_K_CAPS` に**載せていない** ＝ 既定の最も狭い 10。
  「インシデントでもないのに読むな」と本文が言っている一覧に、広い上限を与えない。
- **台帳は開示経路になってはいけない。** ERN では `find_expert` の台帳 `summary` に
  候補者名・`person_id` を入れない（SPEC_phase3 §4f・E13）。入れると
  「誰が誰を探したか」が台帳から読め、E6 の非対称開示が台帳経由で破れる。

#### SFAQ（`questions` / `feedback`）

3軸 ＋ **E-3 許諾ゲート**の AND。どちらが落ちても行は見えない。

- **`candidate` は誰にも配信されない**（`SERVABLE_STATUSES` に無い）。
- E-3: `corporate_training` → `perm_corporate` 必須、それ以外 → `perm_individual` 必須
  （**不明な値は fail-closed**）。
- **E-3(ii) の著者マスク**: `corporate_training ∧ perm_corporate ∧ ¬perm_individual` の行は
  可視のまま**著者名だけ伏せて**配信される。
  → **ERN はこの行をルーティング原料から集計前に落とす**（SPEC_phase3 §4a）。
  落とさないと、伏せたはずの著者が「その領域の専門家」として名前で出る。

#### 改善要望（`improvement_requests`）

**`dept` を既定にする**(SFAQ の起票と同じ)。製品への要望は起票した単位の文脈を持つので、
既定で全社に配らない（`_scope_of` の既定が `ScopeTier.DEPT`）。

- `owner_unit` は `resolve_owner_unit` で確定。**推測しない** — 複数 unit に属する人は
  明示指定を求められ、どこにも属さないなら**拒否**する（org 全体公開へ落ちないため）。
- `steward` と `owner` は横断して見える（トリアージ役）。
  `wontfix` を決められるのは `owner` だけ — **記録する権限と、打ち切る権限は別**。
- `unverified` の行は起票者以外に出ない。

#### ERN 宣言（`ern_assertions`）

**3軸を持たせない判断をした。** レコードの主体はその人自身で、第三者の内容を運ばない。
独立した3軸を与えると「その人を開示してよいか」を決める場所が2つになり、§3.3 が避けている
分裂そのものになる。代わりに `persons` の同意ゲート（`consent_internal` ∧ 在籍 ∧
メンバー資格）に従属させ、SQL 側も `persons` への相関副問い合わせ
（`EXISTS (… WHERE p.person_id = a.person_id AND …)`）で同じ規則を掛ける。

**実際の到達範囲**: 可視な org メンバー全員。つまり
**「自分の unit にだけ領域を宣言する」ことはできない。** 宣言は全社に見える前提で書くもの。

#### ERN 監査（`ern_audit`）

「誰が・いつ・誰を候補にしたか」の append-only 記録。**これを読み出す MCP ツールは作らない**
（SPEC_phase3 §4f）。`record` 以外のメソッドを持たないことをテストで固定してある。

**ただし「ツールが読まない」と「誰も読まない」は別である。**
`<DS_FEEDBACK>` にデータセット読み取り権を持つ人は `SELECT actor_id, topic, candidate_ids`
で「誰が誰を探したか」の全体を再構成できる。**その権限を持つ人が誰かは、この文書でも
どこでも定義されていない**。アプリ層の制御しか無い、というのが現状である。

`topic` は領域ラベルに制限してある（24 字以内・句読点なし。用語の最長がこれに収まることが根拠）。超えるものは `NULL` で残す — 「探されたことは記録され、
何を探したかは残らない」。ただしこれは**仕掛けであって、意図的な迂回を止めるものではない**。

**保持期間**: 365 日（`partition_expiration_days`。`config.ern_audit_retention_days` と揃える）。台帳（`usage_log`）は自動では消さない。

### 2.3 導入形態

本書の既定と追加ゲートは、台帳とデータを BigQuery に置く形態を前提にしている（提供している形態は
これだけである）。§3.3 の二重ゲートのうち「見えない行はそもそも DB から出ない」SQL 側の半分、台帳と
ERN 監査の append-only（IAM で DELETE を禁じる）、意味検索（`VECTOR_SEARCH`）、保持期間の自動失効
（`partition_expiration_days`）は、いずれも BigQuery の機能に依存する。BigQuery を使わない形態を作る
場合は、これらの保証が失われることを前提に設計する。

ERN はベクトル検索を前提にしない。`SPEC_phase3.md` §2.1 は `ern_assertions.embedding` を「**リコール
専用。スコアには一切寄与しない**」と定めており、実装はベクトルを読まない（領域の絞り込みは
`attributes.domain` の一致と本文の部分一致だけ）。3エンジンは feedback 行の重み付き集計＋時間減衰／
単純カウント／監査証跡で、どれも SQL の集計と append で足りる。

### 2.4 例外の一覧（既定から外れる unit / 行）

| 例外 | 対象 | 効果 | 根拠 |
|---|---|---|---|
| `board`（理事会・役員会など） | 全種別 | **org tier へ広げられない**（`widen_refusal`）。人事・契約・係争を扱うため、公開が既定の場に構造的な締めを置く | `acl.py:BOARD_UNIT` |
| `steward` | 改善タスク・改善要望・台帳の横断 | 一覧の横断視界。**`retrieval/acl.py` は広げない** — 用語・SFAQ・名簿は3軸のまま | `improvement/acl.py` |
| `owner` | 改善要望 | `wontfix` を決められる唯一の unit | SPEC_improvement_requests §8 |
| `export_admin` | `_K_CAPS` の超過 | 理由 ＋ 監査 sink があるときだけ。**名簿と `find_expert` は対象外** | 本書 §1 |
| `org` | 全種別 | **owner にしてはいけない**（落とし穴2）。所有権を与えない | `acl.py:ORG_WIDE_UNITS` |
| `sensitive` の行 | 全種別 | `owner_unit` に居るか `allowed_sensitive_units` の明示付与が要る。**org tier へ広げられない** | `acl.py:_sensitivity_ok` / `widen_refusal` |
| `audience=public` の行 | 用語 | **組織外にも届く**（`_audience_ok` は非メンバーにも真） | `acl.py:_audience_ok`・要点検 |

### 2.5 circles（休眠スロット）

`persons.circles` は存在するが**配線されていない**。ERN でも配線しない
（SPEC_phase3 §4d・E12(a)、ERN モジュールは `circles` を import しない）。

将来配線する場合の制約:

- **3軸に AND で従属させる。** circles は絞る方向にしか効かせない。
- **サークル共有を理由に可視範囲を広げてはならない。** 「同じサークルだから見える」を
  作った瞬間、tier / audience / sensitivity のどれも通っていない行が届く経路ができる。

---

## 3. ERN（`find_expert`）がこの表から受け取るもの

| 項目 | 値 | 根拠 |
|---|---|---|
| 1回に返す人数 | **3**（`_K_CAPS["find_expert"]`） | SPEC_phase3 §3 |
| break-glass | **対象外** | 本書 §1・SPEC_phase3 §3 |
| 集計対象の問い | **質問者の可視範囲を継承**（ERN 独自の範囲を持たない） | SPEC_phase3 §4b |
| 名前を出してよい人 | `viewer.is_org_member ∧ person.consent_internal ∧ status='active'` | SPEC_phase3 §4c |
| 除外する原料 | E-3(ii) の著者マスク該当行（集計**前**に落とす） | SPEC_phase3 §4a |
| circles | 使わない | SPEC_phase3 §4d |
| 台帳 `summary` | 候補者名・`person_id` を**入れない** | SPEC_phase3 §4f・E13 |

**名簿が最も狭い上限に乗っている以上、`find_expert` の 3 名はそれよりさらに狭い。**
ERN は「人を返すツール」として、本書の中でいちばん締まった側にある。

---

## 4. 単位名の正典

組織シート（部署コード / 単位名 / 部門・所管）が正典。シート上の名前とユニットIDの対応は
`org.toml` の `[unit_map]` に書き、`scripts/sync_persons_from_sheet.py` が読む。

**シートに無いのに生き残らせる unit**（`manual_units` の carry-forward。ID は `org.toml` の `[units]`）:

- `steward` — 統治役
- `owner` — 改善要望の打ち切り権限
- `export_admin` — break-glass 保有（本書 §1）

これらは手で付与するもので、シートからは導出できない。名簿ファイルから登録する場合は
`scripts/register_members.py` の `roles` で付与する。登録後に `owner` だけを足す場合は `scripts/grant_role_unit.py` を
使う(このスクリプトは `steward` と `export_admin` を扱わない)。
**再同期で消さないこと。**

---

## 5. 本書で決めていないこと

- **`audience=public` の用語**が外向きでよいかの点検の手順。
- **`personal` tier の意味づけ**。実装では `dept` と区別が付かない（落とし穴1）。
  区別を持たせるなら DDL に per-person の所有者列が要る。
- **circles の配線方針**（休眠のまま）。
- **`board` を誰に付けるか**。`acl.py` は名指ししているが、所属する人がいなければ締めは空回りする。
  組織ごとに、理事会・役員会など決定を公開できない単位へ付けること。
- **`ern_audit` と purge**。SPEC_phase3 §5 の purge で人を消しても、その `person_id` は他人の
  `candidate_ids` の中に、パーティションの期限（365 日）が来るまで残る — **purge の削除は完結していない**。
- **`<DS_FEEDBACK>` の BigQuery レベルの読み取り権を誰が持つか。** 上記のとおり、
  `ern_audit` の実効的なアクセス制御はこれであって、「ツールが読まない」ことではない。
- **ERN 役職フォールバック（`ern_role_fallback`）の供給元**。未配線である。配線するときの制約: **著者情報から導出してはならない** —
  組織図・役職から人手で作ること。導出すると、§4a で落としたはずの著者マスク該当者が
  役職ベースの候補として名前で出る経路になる。
