"""Core domain models (stdlib only — safe to import anywhere, no heavy deps).

Mirrors the BigQuery schema in sql/ddl/ (§2.2) and the retrieval interface (§3.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


def utcnow() -> datetime:
    """Default timestamp for in-memory model objects; BigQuery stores may override on insert."""
    return datetime.now(UTC)


class ScopeTier(StrEnum):
    """階層 — organizational hierarchy axis (§3.3)."""

    PERSONAL = "personal"
    TEAM = "team"
    DEPT = "dept"
    ORG = "org"


class ScopeAudience(StrEnum):
    """対内外 — internal/external audience axis (§3.3)."""

    INTERNAL = "internal"
    PARTNER = "partner"
    PUBLIC = "public"


class ScopeSensitivity(StrEnum):
    """機微度 — sensitivity axis (§3.3)."""

    NORMAL = "normal"
    SENSITIVE = "sensitive"


class UseCategory(StrEnum):
    """台帳の利用区分 — **L0/L1/L2 ＋ 禁止列挙** (§2.2/§4.2).

    ポリシーの語彙（L0/L1/L2＋禁止列挙）に揃えている。三色表記も 1:1 で写る:
    🟢→``l2`` / 🟡→``l1`` / 🔴→``l0`` / 🔴床→``prohibited``。入力は
    :func:`waca_loop.guard.normalize_use_category` が三色表記も受け付ける。
    level は許可証ではなく**現在地マーカー**（policy 行の ``level`` と同じ意味）。
    """

    L2 = "l2"                   # そのまま採用してよい（三色表記 🟢）
    L1 = "l1"                   # 条件つき・安全チャネル経由（三色表記 🟡）
    L0 = "l0"                   # いまは使わない・差し控える（三色表記 🔴）
    PROHIBITED = "prohibited"   # 禁止列挙に該当＝下限（三色表記 🔴床）


class Kind(StrEnum):
    TERM = "term"
    PERSON = "person"


@dataclass(frozen=True)
class ScopeTags:
    """The three orthogonal scope tags carried by every grounding record (§2.1)."""

    tier: ScopeTier
    audience: ScopeAudience
    sensitivity: ScopeSensitivity

    def as_dict(self) -> dict[str, str]:
        return {
            "tier": self.tier.value,
            "audience": self.audience.value,
            "sensitivity": self.sensitivity.value,
        }


@dataclass
class Term:
    """A row of ``<DS_GROUNDING>.terms`` (§2.2 ①)."""

    term_id: str
    term: str
    definition: str
    scope: ScopeTags
    reading: str | None = None
    basis: str | None = None
    attributes: dict | None = None
    owner_unit: str | None = None
    embedding: list[float] = field(default_factory=list)
    status: str = "approved"  # approved | draft
    version: int = 1


@dataclass
class Person:
    """A row of ``<DS_GROUNDING>.persons`` (§2.2 ②) — PII, kept separate from terms."""

    person_id: str
    display_name: str
    unit_path: list[str] = field(default_factory=list)
    consent_internal: bool = True   # opt-out default true
    consent_external: bool = False  # opt-in default false
    circles: list[str] = field(default_factory=list)
    status: str = "active"  # active | frozen


@dataclass(frozen=True)
class Viewer:
    """The querying user, used to enforce ACL (§3.3).

    ``units`` is the set of organizational unit ids the viewer belongs to
    (covers personal/team/dept tiers — a personal-tier record's ``owner_unit``
    is a unit only its owner is in). ``allowed_sensitive_units`` are units for
    which the viewer holds an explicit grant to see ``sensitive`` records.
    """

    subject: str                       # OAuth sub (or hashed actor_raw)
    person_id: str | None = None
    is_org_member: bool = False
    is_partner: bool = False
    units: frozenset[str] = frozenset()
    allowed_sensitive_units: frozenset[str] = frozenset()
    # True when ``person_id`` came from ``oauth_identity_map`` (``auth.identity.resolve_viewer``
    # — both production entry points, hosted and stdio, build the viewer there). Tools whose
    # answer depends on *when* this sub was bound to the person (``export_my_usage``,
    # SPEC_export_my_usage §4.1) look the bind time up and fail closed when it is unknown.
    # A viewer built directly in-process (tests, offline dev) has no identity map to ask,
    # so stays False.
    identity_mapped: bool = False

    @staticmethod
    def anonymous(subject: str) -> Viewer:
        """A viewer that resolved no identity — only public/non-sensitive/org-tier."""
        return Viewer(subject=subject)


@dataclass
class TermHit:
    """A search result returned by retrieval and surfaced by ``lookup_terms`` (§4.2)."""

    term_id: str
    term: str
    definition: str
    scope: ScopeTags
    basis: str | None = None
    kind: Kind = Kind.TERM
    owner_unit: str | None = None
    score: float = 0.0

    def to_tool_output(self) -> dict:
        """Shape expected by the ``lookup_terms`` tool output (§4.2)."""
        return {
            "term": self.term,
            "definition": self.definition,
            "basis": self.basis,
            "scope": self.scope.as_dict(),
            "kind": self.kind.value,
        }


class SurveyAudience(StrEnum):
    """Who a survey campaign targets."""

    USER = "user"
    ADMIN = "admin"


class TaskKind(StrEnum):
    """Why an improvement task exists."""

    HARMFUL = "harmful"
    INCIDENT = "incident"
    NPS_DETRACTOR = "nps_detractor"
    TERM_GAP = "term_gap"
    SFAQ_GAP = "sfaq_gap"


class TaskStatus(StrEnum):
    OPEN = "open"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    WONTFIX = "wontfix"


class CandidateKind(StrEnum):
    """What kind of grounding material was extracted."""

    TERM = "term"
    SFAQ = "sfaq"
    ERN = "ern"


class CandidateStatus(StrEnum):
    PROPOSED = "proposed"   # AI proposal awaiting user approval
    APPROVED = "approved"
    REJECTED = "rejected"


def nps_band(score: int) -> str:
    """NPS classification: detractor 0–6, passive 7–8, promoter 9–10."""
    if score <= 6:
        return "detractor"
    if score <= 8:
        return "passive"
    return "promoter"


@dataclass
class SurveyResponse:
    """A periodic-survey response. Summary-only — no verbatim prompt stored.

    Attribution is pseudonymous by default: ``respondent_token`` is a one-way hash of the
    OAuth subject and ``respondent_id`` stays None. In ``identified`` mode the resolved
    ``person_id`` is also kept; in ``anonymous`` mode neither correlates to a person.
    """

    nps_score: int
    nps_reason: str
    useful_summary: str
    harmful_summary: str
    incident_flag: bool
    campaign_id: str                      # survey period, e.g. "2026-06"
    respondent_token: str                 # hash(sub) — pseudonymous
    response_id: str | None = None
    respondent_id: str | None = None      # person_id, only in identified mode
    incident_summary: str | None = None
    contact_opt_in: bool = False          # incident follow-up consent
    ref_log_id: str | None = None         # self-reported usage_log link (helps 特定)
    ref_request_id: str | None = None
    sensitivity: ScopeSensitivity = ScopeSensitivity.NORMAL
    owner_unit: str | None = None
    # Escalation: the reporter asked to route this PAST their own department. Without keeping
    # the intent, `owner_unit` alone reads as "unit could not be resolved" and a steward
    # can hand the report back to the very unit it was routed away from. This is the
    # reporter's routing intent, not new content — the summary-only invariant (§2.2③/T6)
    # is untouched.
    escalated: bool = False


@dataclass
class ImprovementTask:
    """A derived improvement task. The output of 特定 → 課題化."""

    kind: TaskKind
    source_response_id: str
    evidence: str
    task_id: str | None = None
    target_kind: str = "none"             # term | sfaq | usage_log | none
    target_id: str | None = None
    status: TaskStatus = TaskStatus.OPEN
    priority: int = 0                     # higher = more urgent
    owner_unit: str | None = None


class RequestKind(StrEnum):
    """改善要望の種別 (SPEC_improvement_requests §2)。

    ``OTHER`` を残す。取り込みで機械的に bug/improvement へ寄せない —
    寄せると「不具合の件数」を数えたときに混ざっているのに気づけなくなる。
    """

    BUG = "bug"
    IMPROVEMENT = "improvement"
    OTHER = "other"


class RequestStatus(StrEnum):
    """改善要望の状態 (SPEC_improvement_requests §2)。

    ``UNVERIFIED`` はログイン不要のフォーム経由の入口 (§3.2)。本人性が無いので、事務局が
    目で通して ``OPEN`` に上げるまで開発者の作業キュー (§4) には並ばない。
    ``WONTFIX`` だけは付けられる人が違う (§8) — ほかは途中経過の記録で次の更新で直るが、
    これは「直さないと決めた」結論で、起案者への説明責任が伴う。
    """

    UNVERIFIED = "unverified"
    OPEN = "open"
    DESIGNING = "designing"
    IMPLEMENTED = "implemented"
    RELEASED = "released"
    WONTFIX = "wontfix"


@dataclass
class ImprovementRequest:
    """WACA Loop という道具への注文 (SPEC_improvement_requests §2)。

    ``improvement_tasks`` とは別物。あちらは申告・インシデント由来で「組織の仕事」の話、
    こちらは製品への要望。**priority / severity を持たない** — 「最優先で提示」は数値では
    なく提示順のこと (§4)。逐語も持たない (INV-1)。
    """

    kind: RequestKind
    title: str
    body: str
    channel: str = "mcp"                  # mcp | form
    request_id: str | None = None
    reporter_id: str | None = None        # 匿名（名前空欄）なら None
    reporter_label: str | None = None     # 表示名 or "匿名"
    status: RequestStatus = RequestStatus.OPEN
    owner_unit: str | None = None
    # 3軸スコープ。ほかのレコード種別と同じ形にして、可視性の判定を retrieval/acl.py に
    # そのまま任せる（要望専用の ACL 方言を作らない）。既定は「部内・組織内・通常」。
    scope: ScopeTags = field(
        default_factory=lambda: ScopeTags(
            ScopeTier.DEPT, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL
        )
    )
    issue_url: str | None = None
    resolution: str | None = None


@dataclass
class ExtractionCandidate:
    """A term / SFAQ / ERN candidate extracted by the AI, awaiting user approval.

    The AI proposes ``payload`` and a suggested sharing scope (hierarchy + circles); the user
    approves before anything reaches the grounding layer (AI提案＋利用者承認).
    """

    kind: CandidateKind
    payload: dict                         # term/def | question/decision/reason | person/topic
    proposed_scope: ScopeTags
    candidate_id: str | None = None
    source: str = "survey"                # survey | prompt | summary
    source_ref: str | None = None
    proposed_circles: list[str] = field(default_factory=list)
    owner_unit: str | None = None
    status: CandidateStatus = CandidateStatus.PROPOSED


@dataclass
class UsageRecord:
    """A row destined for ``<DS_LEDGER>.usage_log`` (§2.2 ③).

    NOTE: there is intentionally **no verbatim-prompt field** — only ``summary``.
    This structurally enforces acceptance criterion T6 (要約のみ).
    """

    use_category: UseCategory
    data_kinds: list[str]
    summary: str
    client: str
    actor_id: str | None = None   # persons.person_id when resolved
    actor_raw: str | None = None  # hashed OAuth sub when unresolved
    request_id: str | None = None
    ingested_via: str = "mcp"     # mcp | hook
    policy_row_ids: list[str] | None = None  # policy rows this usage was judged under (§9.3)
    policy_version: int | None = None        # max version of the applicable rows at log time
    # SPEC_ops R2: recorded (default) | pending (org-context unconfirmed, steward reviews)
    # | discarded is never stored — discard deletes the row physically.
    status: str = "recorded"


@dataclass(kw_only=True)
class PolicyRule:
    """A row of the org's AI policy (SPEC_phase1 §9). Human-authored; WACA Loop only serves it
    (配達係).

    ``kw_only``: keyword-only construction makes any positional call a loud TypeError
    instead of silently mis-binding ``line``/``effect``/``level`` if fields are reordered.

    ``scope_unit == "org"`` is the org-wide floor; otherwise it applies to that owner_unit.
    ``is_floor`` marks the absolute bright-lines the org fixes (not overridable downward);
    floor rows carry no ``level``. Business rows carry the 採用レベル ``level``
    (0=未使用 / 1=条件付き / 2=そのまま採用) — a current-adoption marker, not a
    permission color.
    """

    row_id: str                 # human policy row id, e.g. "KYO-6" (stable key)
    scope_unit: str             # owner_unit; "org" = org-wide floor
    line: str                   # 「〜すること」 — the action the row governs
    effect: str = ""            # 「どう効かせる/迂回するか」 — detour / condition
    level: int | None = None    # 採用レベル 0/1/2 (business rows); None for floor rows
    is_floor: bool = False
    status: str = "approved"    # approved | trial | draft
    version: int = 1
    trigger_kinds: list[str] = field(default_factory=list)  # data_kinds that fire this row (§9.4)


class Provenance(StrEnum):
    """Where a question came from — drives the E-3 permission gate (SPEC_phase2 §2.3)."""

    CORPORATE_TRAINING = "corporate_training"
    INDIVIDUAL = "individual"
    OTHER = "other"


class QuestionStatus(StrEnum):
    """SFAQ state machine: 生ログ→候補→暫定→承認（＋係争中） (SPEC_phase2 §2.4)."""

    CANDIDATE = "candidate"
    PROVISIONAL = "provisional"
    APPROVED = "approved"
    CONTESTED = "contested"


class FbType(StrEnum):
    """The six feedback types, AI-classified — humans never label (S5)."""

    DECISION = "decision"
    AGREE = "agree"
    REFUTE = "refute"
    SUPPLEMENT = "supplement"
    OPEN = "open"
    USEFUL = "useful"


@dataclass
class Question:
    """A row of ``<DS_KNOWLEDGE>.questions`` (SPEC_phase2 §2.1) — the SFAQ center object.

    ``owner_unit`` is an implementation column beyond the SPEC_phase2 §2.1 DDL: the 3-axis ACL
    (§3.3) needs it to gate personal/dept tiers, exactly like ``terms.owner_unit``.
    A personal-tier row's owner_unit is a unit only its owner belongs to.
    """

    question_id: str
    question_text: str
    scope: ScopeTags
    provenance: Provenance
    owner_unit: str | None = None
    attributes: dict | None = None
    cluster_id: str | None = None
    auto_merge: bool = False                  # S6 — default off; only human-approved clusters
    perm_corporate: bool | None = None        # E-3 corporate permission (None = unconfirmed)
    perm_individual: bool = False             # E-3 individual permission (migration default false)
    status: QuestionStatus = QuestionStatus.CANDIDATE
    source: str | None = None                 # import job name | mcp ...
    embed_text: str | None = None             # 検索が見る文字列（見出し＋本文）— 下記
    embedding: list[float] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def search_text(self) -> str:
        """検索（字面・ベクトルの両方）が対象にする文字列。

        `question_text` は**見出し**であって、組織の学びそのものではない。学びの中身は
        `feedback(decision)` に入っている。見出しだけを埋め込むと、検索の当たり外れが
        見出しを作った正規表現の出来に丸ごと依存する — 外部の記録から取り込むとき、規則が
        当たらなかった行は「〜である。— このとき、どう判断するか？」のような見出しになる。

        `embed_text` に「見出し＋本文」を持たせ、検索はこちらを見る。文体の揺らぎ
        （である体／常体、「傾向がある」と「傾向にある」）はベクトルが吸収するので、
        **揺らぎはベクトルの側で吸収する**。見出しは派生物になり、
        規則を直して作り直せる — コーパスを触らずに品質を上げられる。

        未設定なら見出しを使う（embed_text を持たない行もそのまま検索できる）。
        """
        return self.embed_text or self.question_text


@dataclass
class Feedback:
    """A row of ``<DS_KNOWLEDGE>.feedback`` — accrues on a question (SPEC_phase2 §2.1)."""

    feedback_id: str
    question_id: str
    fb_type: FbType
    body: str | None = None
    author_id: str | None = None              # masked at serve time under E-3(ii)
    created_at: datetime = field(default_factory=utcnow)
