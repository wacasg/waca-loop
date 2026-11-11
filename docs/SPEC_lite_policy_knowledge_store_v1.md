# 仕様: Lite 版 ポリシー・知識ストア v1

位置づけ: ポリシー・知識ストアの backend 抽象化の仕様。Lite は小規模向けの簡易な配布形態で、
本仕様の機能を Sheets だけで動かす。

## 0. 目的

WACA Loop 本体の次の価値は、台帳だけではなく「AI を安全に使う次の一歩」を返すことにある。
この仕様では、その機能を Lite でも落とせるよう、Docs / Sheets / BigQuery の差を backend
実装に閉じる。機能の定義は WACA Loop 本体(Standard)が正本であり、Lite はそれを簡易化したものである。

Docs にはガイドライン的なものと活用に関する方針がある。Sheets には部署ごと、個人ごとの
活用状況、目標、課題、指導内容、本人の希望がある。WACA Loop はこの2つを混ぜて保存せず、
Docs を判断の上位ルール、Sheets を現場の現在地として読み、実行時に組み合わせる。

この仕様は、次の2つの機能を定める。

1. `PersonalCoach`: 本人に次の一歩を返す。
2. `CandidateMiner`: TERMS/SFAQ/ポリシー改訂候補を出す。

## 1. 原則

- WACA Loop 本体の仕様が正である。Lite(Sheets を source とする方針・活用状況の読み込み)は本体の上に載る。
- Lite 固有の制約を、WACA Loop 本体の機能要件へ逆流させない。
- Docs は方針の **input source of record** である。
- Sheets は活用状況の **input source of record** である。
- `usage_log`、改善要望、監査・取り下げの正は WACA Loop 側の store である。Sheets への出力は
  **output mirror** であり、同一タブ・同一列で双方向化しない。
- WACA Loop は正本を承認しない。読み、検証し、正規化し、配達し、候補を出す。
- 活用レベル 0/1/2 は許可・禁止ではなく、現在地と目標である。
- 共通下限と安全経路は、活用レベルとは別軸で扱う。
- raw prompt / transcript / verbatim は保存しない。
- 本人向け推薦は評価、査定、ランキングではなく、本人の業務改善と安全な活用支援である。
- GitHub Issue は自動作成しない。作る場合は管理者が押す。

## 2. Store 分割

### 2.1 `PolicyGuideStore`

Docs 由来の上位方針を持つ。

責務:

- 全社 AI ポリシーを読む。
- 組織の AI ポリシーを構成する文書(基本方針・組織内向け・組織外向けなど)を source として扱う。
- 共通下限を返す。
- 情報の取り扱い 3 経路を返す。
- WACA Loop を安全チャネルとして扱える条件と状態を返す。
- 発効日前の文書は `trial` として扱う。
- 正本に書かれた頻度で再読込する。

### 2.2 `AdoptionMapStore`

Sheets 由来の部署別・個人別の活用マップを持つ。

責務:

- 部署別タブを読む。
- 個人別タブ、または個人別 Sheets を読む。
- タブ名またはヘッダから `scope_unit` / `actor_scope` を解決する。
- 現状レベル、目標レベル、課題、上長指導内容、本人希望を返す。
- `LL共有範囲` を ACL へ写す。
- 正本に書かれた頻度で再読込する。

### 2.3 `KnowledgeCandidateStore`

TERMS/SFAQ/ポリシー改訂候補を持つ。既存の `ExtractionCandidate` と `CandidateStore` を
できるだけ再利用する。

責務:

- TERMS 候補を保存する。
- SFAQ 候補を保存する。
- ポリシー改訂候補を保存する。
- red team や検索失敗から出た候補を保存する。
- 承認前の候補を正本として配達しない。

## 3. データモデル

### 3.1 `PolicyGuide`

Docs 由来の上位方針。

```python
@dataclass
class PolicyGuide:
    guide_id: str
    source_id: str
    source_type: str  # design_philosophy | internal_policy | external_policy | member_policy
    title: str
    summary: str
    status: str       # draft | trial | approved | retired
    version: int
    effective_at: datetime | None
    review_frequency: str | None
    source_ref: str
```

`summary` は要約であり、Docs 全文のコピーではない。

### 3.2 `PolicyFloor`

共通下限。

```python
@dataclass
class PolicyFloor:
    row_id: str
    line: str
    effect: str
    trigger_kinds: list[str]
    source_ref: str
    status: str
    version: int
```

既存 `PolicyRule(is_floor=True)` に写せる形を保つ。

### 3.3 `AdoptionRow`

Sheets の部署別・個人別活用行。

```python
@dataclass
class AdoptionRow:
    row_id: str
    scope_unit: str
    actor_scope: str | None
    business: str
    current_level: int
    target_level: int | None
    current_usage: str
    safe_practice: str
    issue_summary: str | None
    next_step: str | None
    owner_label: str | None
    manager_guidance: str | None
    person_intent: dict
    share_scope: str
    updated_at: datetime | None
    status: str
    source_ref: str
    version: int
```

`actor_scope` は個人別 policy の対象者を表す。raw OAuth sub は保存しない。
組織のメールアドレスは運用上の主体IDとして扱い、許可された workspace domain に限って
`actor_scope` に保持できる。それ以外のメールアドレスや OAuth subject 由来の値は行ごと落とす。

`LL共有範囲` は空欄なら、個人行（`actor_scope` あり）は `person`、部署行は `unit` とする。
既知値（本人のみ、部署、全社、上長、管理者など）は正規化する。
未知値は fail-closed として `person` に丸める。`manager` / `admin` scope の個人行を他者へ出す
権限は `steward` のみに限定する。
メールアドレスを `actor_scope` に使う場合も、`row_id` や推薦の source id にはメールをそのまま
載せず、安全参照に丸める。

`person_intent` の許可キー:

- `wanted_work`
- `try_next`
- `concern`
- `improvement_request`

### 3.4 `SafeChannel`

WACA Loop や他 AI 実行環境を安全チャネルとして扱えるかを表す。

```python
@dataclass
class SafeChannel:
    channel_id: str
    scope_unit: str
    status: str  # unreviewed | trial | approved | suspended
    allowed_data_kinds: list[str]
    required_controls: list[str]
    approved_by_role: str | None
    approved_at: datetime | None
    source_ref: str | None
```

## 4. プロトコル

### 4.1 `PolicyGuideStore`

```python
class PolicyGuideStore(Protocol):
    def guides_for(self, viewer: Viewer) -> list[PolicyGuide]: ...
    def floors_for(self, viewer: Viewer) -> list[PolicyFloor]: ...
    def safe_channels_for(self, viewer: Viewer) -> list[SafeChannel]: ...
    def refresh_due_sources(self, now: datetime) -> list[str]: ...
    def refresh_source(self, source_id: str) -> RefreshResult: ...
```

### 4.2 `AdoptionMapStore`

```python
class AdoptionMapStore(Protocol):
    def rows_for_person(self, viewer: Viewer) -> list[AdoptionRow]: ...
    def rows_for_unit(self, viewer: Viewer, scope_unit: str | None = None) -> list[AdoptionRow]: ...
    def refresh_due_sources(self, now: datetime) -> list[str]: ...
    def refresh_source(self, source_id: str) -> RefreshResult: ...
```

### 4.3 `KnowledgeCandidateStore`

既存 `InMemoryCandidateStore` / `BigQueryCandidateStore` の surface に寄せる。

```python
class KnowledgeCandidateStore(Protocol):
    def add(self, candidate: ExtractionCandidate) -> str: ...
    def get(self, candidate_id: str) -> ExtractionCandidate | None: ...
    def list(self, *, status: str | None = None) -> list[ExtractionCandidate]: ...
    def set_status(self, candidate_id: str, status: CandidateStatus) -> None: ...
```

### 4.4 `RefreshResult`

```python
@dataclass
class RefreshResult:
    source_id: str
    changed: bool
    success: bool
    message: str
    source_version: str | None
    checksum: str | None
    refreshed_at: datetime
```

再読込失敗時は直前の有効版を使い、管理者向け確認に失敗を残す。

## 5. `PersonalCoach`

### 5.1 役割

`PersonalCoach` は、本人に「安全に試せる次の一歩」を返す。

本人向け推薦は2経路で出す。

- WACA Loop に聞いたときに、その場で返す。
- 定期的に本人別 Sheets または本人向けアウトプットへ書き出す。

### 5.2 入力

```python
@dataclass
class CoachRequest:
    viewer: Viewer
    task_hint_summary: str | None
    mode: str  # on_demand | scheduled
    max_items: int = 3
```

`task_hint_summary` は要約だけであり、raw prompt ではない。

`PersonalCoach` が読むもの:

- `PolicyGuideStore.guides_for(viewer)`
- `PolicyGuideStore.floors_for(viewer)`
- `PolicyGuideStore.safe_channels_for(viewer)`
- `AdoptionMapStore.rows_for_person(viewer)`
- `AdoptionMapStore.rows_for_unit(viewer)`
- `UsageLogStore` の要約済み利用記録（§5.7 の read surface に限る）
- TERMS/SFAQ の検索結果または候補

### 5.3 出力

```python
@dataclass
class CoachRecommendation:
    recommendation_id: str
    target: str                 # self
    title: str
    reason: str
    next_step: str
    suggested_level: int | None
    safety_path: str | None     # 安全な進め方の文(行の記述、または必要な統制から作る)
    required_controls: list[str]
    detour: str | None
    source_policy_row_ids: list[str]
    source_adoption_row_ids: list[str]
    source_question_ids: list[str]
    source_term_ids: list[str]
    output_channel: str         # on_demand | scheduled
    status: str                 # proposed | dismissed | accepted | done
```

返す tool output は dict でよい。

```json
{
  "recommendations": [
    {
      "title": "問い合わせ回答案を安全にテンプレ化する",
      "next_step": "会員情報を削った要約で回答案を作り、規約と過去対応を人が照合する",
      "safety_path": "prepare",
      "required_controls": ["pii_scrub", "human_review"],
      "source_policy_row_ids": ["FLOOR-01", "SEC-2"],
      "source_adoption_row_ids": ["SEC-1"]
    }
  ],
  "not_for_evaluation": true
}
```

### 5.4 推薦ロジック

推薦は決定木で行う。判定部分は差し替えられる形にしておく(LLM による判定に置き換えられる)。

優先順位:

1. 本人がやりたい、または試したいと書いた業務。
2. 現状レベルより目標レベルが高い業務。
3. usage_log があり、定型化・テンプレ化できそうな業務。
4. 同部署で類似の成功パターンがある業務。
5. L0 だが、迂回路が明確な業務。

除外:

- 共通下限に触れ、迂回路が作れないもの。
- 安全チャネルが `suspended` の対象データ。
- 本人や自部署に見せられない scope のもの。
- 取り下げ済み usage_log だけを根拠にするもの。

### 5.5 定期アウトプット

定期アウトプットは、本人別 Sheets または本人向け出力先へ書く。

最低限の列:

- `recommendation_id`
- `created_at`
- `target_person_id`
- `title`
- `next_step`
- `safety_path`
- `required_controls`
- `source_policy_row_ids`
- `source_adoption_row_ids`
- `status`

本人が `dismissed` / `accepted` / `done` を返せる形にする。これは評価ではなく、次の推薦を
良くするための状態である。

### 5.6 PolicyWorkbench: ログ参照型の対話コーチ

`PersonalCoach` の上位に、ログを見て成果物作成まで対話する `PolicyWorkbench` を置く。
これは単なる「おすすめ一覧」ではない。直近の利用ログから本人・部署のAI活用の現在地を読み、
ポリシー上のあるべき姿との差分を見つけ、プログラム、資料、マニュアル、チェックリスト、
テンプレートなどの成果物作成へつなげる。

`PolicyWorkbench` が読むもの:

- `UsageLogStore` の要約済み利用記録（本人、自部署、閲覧可能な範囲のみ）
- `PolicyGuideStore` / `AdoptionMapStore` の現在の正本
- TERMS/SFAQ の検索結果または候補
- 直近セッションでユーザーが選んだ成果物種別、進行中の作業メモ

`PolicyWorkbench` は raw prompt / transcript / verbatim を読まない。台帳にある summary と
構造化メタデータだけを使う。

出力は、次の順に組み立てる。

1. **最近のAI利用の現在地**: 「最近は問い合わせ回答、手順整理、コード作成に使っている」など。
2. **今回の業務に沿ったポリシー説明**: `SEC-2 / FLOOR-01 / ORG-4` のようなID列ではなく、
   「個別問い合わせは、内容要約と回答案作成には使える。送信前に規約・過去対応を人が照合する」
   のように業務の言葉へ翻訳する。
3. **あるべき姿**: 単発質問で終わらせず、再利用できる成果物にする方向を出す。
4. **今日作るものの候補**: `program` / `document` / `manual` / `checklist` / `template`。
5. **最初の質問**: 成果物を作るための次の1問だけを返す。

下限（共通禁止線）は、通常の推薦文では列挙しない。下限は内部判定として常に効かせるが、
ユーザーに出すのは次の場合だけ。

- 入力内容または作ろうとしている成果物が、下限に触れる可能性がある。
- 安全な迂回路を選ばないと作業が進められない。
- ユーザーが「なぜこの入力ではだめか」「どこまで入れてよいか」を明示的に聞いた。

その場合も「氏名、メール、会員番号を入れない」のような汎用注意を毎回出さず、今回の業務に沿って
短く言い換える。

例:

```text
最近のログでは、問い合わせ窓口の運用と手順整理にAIを使っています。
この業務は、運用マニュアルやチェックリストの作成には使えます。認証情報そのものを扱う必要はなく、
架空の画面名・操作名で手順を固めてから実環境で人が確認する形がよいです。

今日作るなら「問い合わせ一次対応チェックリスト」が一番効果が出そうです。
まず、問い合わせを3分類するとしたら「ログイン」「申込状況」「請求」で合っていますか？
```

根拠IDは machine-readable な `source_policy_row_ids` には残す。ただし通常の user-facing text では、
IDの列挙だけを根拠として表示しない。

### 5.7 UsageLog の直近の読み出し

`PolicyWorkbench` と `PersonalCoach` が読む台帳面は、既存 `log_usage` の書き込み面とは別に
定義する。台帳は要約のみを保存するが、要約であっても本人の業務内容を含むため、読み取り面は
狭く固定する。

読み出し面(`ledger/usage_log.py`。InMemory と BigQuery の両方に実装):

```python
class UsageSummaryReader(Protocol):
    def recent_summaries_for_actor(
        self,
        viewer: Viewer,
        actor_id: str,
        *,
        limit: int = 20,
    ) -> list[UsageSummary]: ...
```

部署単位の読み出しは提供しない。追加する場合も、下の不変条件(閲覧可能 unit に限る、
`min_subjects` 未満の部署集約は返さない)を満たすこと。

不変条件:

- raw prompt / transcript / verbatim を返さない。
- withdrawn 済みの `usage_log` は返さない。
- `viewer` 必須。actor/unit を文字列だけで指定する読み取り口を作らない。
- 本人読み取りは、本人または管理者権限に限る。
- 部署読み取りは、閲覧可能 unit に限る。必要なら `min_subjects` 未満の部署集約は返さない。
- BigQuery 実装では SQL WHERE と in-process の二重ゲートを維持する。
- InMemory / BigQuery / Sheets backend で同じテストを通す。

返す `UsageSummary` は、最低限次の形にする。

```python
@dataclass(frozen=True)
class UsageSummary:
    log_id: str
    occurred_at: datetime
    actor_id: str | None
    owner_unit: str | None
    use_category: str
    data_kinds: list[str]
    summary: str
    policy_row_ids: list[str]
```

この read surface の追加は、台帳を評価・査定・ランキングに使うためではない。本人または部署が
自分たちのAI活用を改善するための材料に限る。

### 5.8 Feedback intake の内部振り分け(未実装)

本節は設計のみで、コードには未実装である。

`feedback_intake` は UI/API 上の入口名であり、保存先の名前ではない。入口を1つにしても、
内部では `intake_kind` で必ず分ける。

```python
class FeedbackIntakeKind(StrEnum):
    SURVEY_RESPONSE = "survey_response"
    WACA_LOOP_PRODUCT_REQUEST = "product_request"
    RED_TEAM_RESULT = "red_team_result"
    KNOWLEDGE_CANDIDATE_SIGNAL = "knowledge_candidate_signal"
    POLICY_REVISION_SIGNAL = "policy_revision_signal"
```

振り分け:

| `intake_kind` | 保存先 | 既存機能との関係 |
|---|---|---|
| `survey_response` | `survey_responses` / `improvement_tasks` | `submit_survey_response` の経路 |
| `product_request` | `improvement_requests` | `report_improvement_request` の経路。WACA Loop という道具への注文 |
| `red_team_result` | red team 専用 signal または `improvement_tasks` | 通常サーベイと混ぜない |
| `knowledge_candidate_signal` | `knowledge_candidates` | `CandidateMiner` に渡す要約済み signal |
| `policy_revision_signal` | policy revision candidates | SFAQ に混ぜない |

`feedback_intake` は保存先を隠すための名前ではない。保存先と処理キューが違うものを1テーブルに
潰さない。特に WACA Loop 製品への不具合・改善要望は SFAQ 候補や組織の運用サーベイに混ぜない。

## 6. `CandidateMiner`

### 6.1 役割

`CandidateMiner` は、TERMS/SFAQ/ポリシー改訂候補を出す。候補は承認されるまで正本ではない。

### 6.2 入力

```python
@dataclass
class CandidateMiningRequest:
    viewer: Viewer
    source_kind: str  # usage_log | adoption_row | red_team | search_miss | feedback
    source_ref: str
    summary: str
    policy_row_ids: list[str]
    owner_unit: str | None
```

`summary` は要約のみ。red team の場合も、本物の PII や機密は保存しない。

### 6.3 出力

既存 `ExtractionCandidate` を使う。

TERM payload:

```json
{
  "term": "安全チャネル",
  "definition": "組織が条件を確認し、扱えるデータと統制を定めたAI利用経路",
  "basis": "policy_source:<source_id>"
}
```

SFAQ payload:

```json
{
  "question_text": "会員番号を含む問い合わせをAIに相談するときはどう加工するか",
  "judgment": "氏名・会員番号などを削除または仮名化し、必要なら要約だけを使う",
  "fb_type": "decision",
  "source_refs": ["policy:FLOOR-01", "adoption:SEC-2"],
  "support_count": 1
}
```

POLICY payload:

```json
{
  "policy_gap": "L1条件が部署タブでは曖昧",
  "suggested_action": "安全な進め方に匿名化と人確認を明記する",
  "source_refs": ["adoption:SEC-2"]
}
```

`CandidateKind` は `TERM` / `SFAQ` / `ERN` の3種で、policy 改訂候補は `CandidateKind.SFAQ` ではなく、
別の `policy_candidates` 行として扱う。

`CandidateMiningResponse` は全候補の `candidate_ids` に加え、`term_candidate_ids` と
`sfaq_candidate_ids` を分けて返す。TERMS/SFAQ の候補数や承認導線を別々に扱うためである。

### 6.4 候補化ルール

TERM 候補:

- 未定義語が繰り返し出る。
- 検索失敗が同じ語で複数回出る。
- policy や adoption row の判断に必要な語が TERMS に無い。

SFAQ 候補:

- 同じ判断が複数回出る。
- 迂回路が何度も説明される。
- red team で検知した安全判断を再利用できる。
- 本人や部署が同じ不安を繰り返す。

Policy 改訂候補:

- Docs 方針と Sheets の業務行が矛盾する。
- L1条件が曖昧で記録が揺れる。
- safety_path が決められない。
- safe channel status が不明なまま扱われている。

### 6.5 承認

- TERMS/SFAQ 候補は既存の候補承認フローに乗せる。
- 承認はスチュワードまたは定められた管理者が行う。
- 承認前の候補は `lookup_terms` / `search_sfaq` の正本として配達しない。
- GitHub Issue 化はしない。必要なら issue draft を作り、管理者が押す。

## 7. MCP / ServerContext 追加

Lite 版で ServerContext に加わるメソッド。

### 7.1 `get_personal_coach`

入力:

```python
def get_personal_coach(task_hint_summary: str | None = None, max_items: int = 3) -> dict: ...
```

出力:

- `target_person_id`
- `mode`
- `not_for_evaluation`
- `recommendations`(各推薦が `source_policy_row_ids` を持つ)
- `coverage_note` 任意(未実装)

### 7.2 `mine_knowledge_candidates`

MCP tool としては公開せず、内部から呼ぶ。検索失敗、改善要望、red team、定期バッチなど、
WACA Loop 内部で要約済みの signal だけを渡す。

```python
def mine_knowledge_candidates(
    source_kind: str,
    source_ref: str,
    summary: str,
    policy_row_ids: list[str] | None = None,
) -> dict: ...
```

`summary` は候補化の判定に使うが、候補 store にそのまま保存しない。保存するのは
`source_kind`、安全化した `source_ref`、候補ラベル、確認理由、policy row id などの
構造化フィールドに限る。URL token、メール、逐語的な red team payload が `source_ref` に
入った場合は、ハッシュ化した参照IDに丸める。

返すもの:

- `candidate_ids` — 作成した全候補の ID
- `term_candidate_ids` / `sfaq_candidate_ids` — TERM/SFAQ の切り分け用
- `policy_candidates` — ポリシー改訂の候補

### 7.3 `export_personal_recommendations`

定期アウトプット用。定期実行から呼ぶ。

```python
def export_personal_recommendations(max_items: int = 3) -> dict: ...
```

返すもの:

- `target_person_id`
- `not_for_evaluation`
- `rows`(推薦1件につき1行。`recommendation_id`・`title`・`next_step`・`safety_path`・
  `required_controls`・根拠の行ID・`status` など)

Sheets へは直接書き込まず、`rows` を含む dict を返す。書き込みは呼び出し側が行う。
出力前にも `ServerContext` 側で本人IDの二重ゲートをかけ、`not_for_evaluation` を付ける。

### 7.4 `start_policy_workbench`

本人または管理者が「直近ログから、何を安全に作るべきか」を聞いたときに使う。
`get_personal_coach` は推薦一覧、`start_policy_workbench` は対話セッション開始である。

入力:

```python
def start_policy_workbench(
    goal_hint_summary: str | None = None,
    artifact_type: str | None = None,
    max_logs: int = 20,
) -> dict: ...
```

`goal_hint_summary` は要約だけであり、raw prompt ではない。`artifact_type` は
`program` / `document` / `manual` / `checklist` / `template` / `unspecified` のいずれか。

出力:

- `session_id`
- `current_state`: 直近ログから見たAI利用の現在地
- `desired_state`: ポリシー上・運用上のあるべき姿
- `recommended_artifact`: 今日作る成果物候補
- `policy_explanation`: 今回の業務に沿った短い説明
- `first_question`: 対話を進めるための1問
- `source_policy_row_ids`
- `source_log_ids`
- `lower_bound_note` 任意（下限に触れそうな場合だけ）
- `not_for_evaluation`

例:

```json
{
  "current_state": "最近は問い合わせ窓口の運用の調査と手順整理にAIを使っています。",
  "desired_state": "毎回の相談を、再利用できる問い合わせ一次対応チェックリストにします。",
  "recommended_artifact": {
    "type": "checklist",
    "title": "問い合わせ一次対応チェックリスト"
  },
  "policy_explanation": "問い合わせ窓口の運用は、手順整理・チェックリスト作成には使えます。実環境の秘密情報を入れず、架空の操作例で作ってから人が確認します。",
  "first_question": "問い合わせを3分類するとしたら「ログイン」「申込状況」「請求」で合っていますか？",
  "source_policy_row_ids": ["SEC-5", "SEC-18"],
  "source_log_ids": ["..."],
  "not_for_evaluation": true
}
```

未設定時のふるまい:

- `policy_guide_store` / `adoption_map_store` / usage log reader の未設定エラーを user-facing text に
  出さない。
- 使える情報だけで暫定診断を返し、`diagnostic` に `partial` を入れる。
- 内部名（`policy_guide_store/adoption_map_store not configured` など）はログ・管理者向け確認に閉じる。

## 8. Backend 方針

### 8.1 InMemory

テストの基準になる実装である。

- `InMemoryPolicyGuideStore`
- `InMemoryAdoptionMapStore`
- 既存 `InMemoryCandidateStore`

### 8.2 Sheets

Sheets を source とする実装。Google Sheets API は実装内に閉じる。

- Docs / Sheets の source を `policy_sources` から読む。
- adoption tab は `policy_sources` に `source_type=policy_tab` などで明示されたタブだけ読む。
  未登録タブは警告して無視する。
- 個人別 policy が非公開なら、master Sheets ではなく個人別 Sheets を source として持つ。
- タブ単位の閲覧制御を安全な前提にしない。
- 読み込み失敗時は直前の有効版を使う。例外詳細や URL token は user-facing message に出さない。
- `source_ref` は安全な短い参照IDに正規化し、URL、メール、自由文はハッシュ化する。
- 日付セルは UTC-aware datetime に正規化する。オフセット無しの時刻付き文字列も UTC として扱う。
- `refresh_frequency` があり `last_refreshed_at` が無い source は、初回読込対象として due にする。

### 8.3 BigQuery

**未実装**。BigQuery 版の `PolicyGuideStore` / `AdoptionMapStore` は無く、現在は InMemory と
Sheets(`LiteSheetsPolicyStore`)のみ。実装する場合は Sheets と同じ Protocol を実装する。

- SQL 方言は BigQuery 実装内に閉じる。
- ACL は SQL WHERE と in-process の二重ゲートを維持する。
- 同じテストを InMemory / Sheets / BigQuery で通す。

## 9. 受け入れ条件

- PKS-1: Docs 由来の方針と Sheets 由来の活用行を別モデルとして保持する。
- PKS-2: 発効日前の正本は `trial` として配達される。
- PKS-3: 活用レベル 0/1/2 は許可分類ではなく現在地・目標として扱われる。
- PKS-4: 共通下限と安全経路は活用レベルと別軸で判定される。
- PKS-5: `PersonalCoach` は本人に推薦を返す。
- PKS-6: 本人向け推薦は評価、査定、ランキングとして表示されない。
- PKS-7: 本人の希望と上長指導内容を推薦に使える。
- PKS-8: WACA Loop に聞いた時と定期アウトプットの両方に対応する。
- PKS-9: `CandidateMiner` は TERMS 候補と SFAQ 候補を分けて出す。
- PKS-10: 候補は承認前に TERMS/SFAQ 正本として配達されない。
- PKS-11: raw prompt / transcript / verbatim を保存しない。
- PKS-12: 個人別 policy を非公開にしたい場合、個人別 Sheets を `policy_sources` でリンクできる。
- PKS-13: 再読込失敗時は直前の有効版を使い、管理者向け確認に失敗を残す。
- PKS-14: BigQuery 方言は Protocol と ServerContext に出ない。
- PKS-15: `PolicyWorkbench` は直近ログから現在地を要約し、ポリシー上のあるべき姿との差分を出す。
- PKS-16: user-facing text は根拠IDの列挙だけにせず、今回の業務に沿った短い説明を返す。
- PKS-17: 下限は内部判定として常時効かせ、通常は列挙しない。触れそうな時だけ
  `lower_bound_note` として今回の業務に沿って出す。
- PKS-18: `PolicyWorkbench` はプログラム、資料、マニュアル、チェックリスト、テンプレートの
  いずれかの成果物作成へ進む最初の1問を返す。

## 10. 未決事項

- 個人別推薦の定期アウトプット先の初期形(本人別の Sheets か、一覧からリンクされた個人別の Sheets か)。
- policy 改訂候補のために `CandidateKind.POLICY` を設けるか、別のストアにするか。
- 安全チャネルの条件の正本を、どの行で管理するか。
- `PolicyWorkbench` の対話セッションの状態を、メモリ内だけに置くか、Sheets / BigQuery に保存するか。
