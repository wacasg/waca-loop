"""MCP server — registers the tools (§4): lookup_terms, log_usage, get_company_context.

The tool logic lives on :class:`ServerContext` (plain, directly unit-testable). :func:`build_mcp`
binds those methods onto a FastMCP server; :func:`main` wires the production BigQuery-backed
context from settings + OAuth (§5).
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from waca_loop.embeddings import Embedder
from waca_loop.grounding.persons import hash_subject
from waca_loop.improvement.candidates import approve_candidate as _approve_candidate
from waca_loop.improvement.identify import identify_content
from waca_loop.improvement.tasks import make_tasks_from_response
from waca_loop.models import (
    CandidateKind,
    CandidateStatus,
    ExtractionCandidate,
    Kind,
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    SurveyResponse,
    TermHit,
    UsageRecord,
    UseCategory,
    Viewer,
)
from waca_loop.retrieval.acl import ORG_UNIT, STEWARD_UNIT
from waca_loop.retrieval.hybrid import TermStore, hybrid_search
from waca_loop.signature.prompt import SYSTEM_PROMPT_RULES
from waca_loop.survey.scheduler import current_campaign_id
from waca_loop.survey.store import incident_sensitivity

# The periodic survey question set. Delivered MCP-internally; the user may skip.
SURVEY_QUESTIONS = [
    {"id": "nps", "type": "int", "range": [0, 10],
     "text": "このAIを同僚に勧める可能性は？（0–10）"},
    {"id": "nps_reason", "type": "text", "text": "その理由を一言で。"},
    {"id": "useful", "type": "text",
     "text": "特に有用だったAIの回答があれば要約で。"},
    {"id": "harmful", "type": "text",
     "text": "有害・誤りだったAIの回答があれば要約で。"},
    {"id": "incident_flag", "type": "bool",
     "text": "AIの情報がインシデントになりうると感じた件は？"},
    {"id": "incident_summary", "type": "text",
     "text": "あれば内容を要約で（機微情報として扱います）。"},
]


logger = logging.getLogger(__name__)

# Per-tool result ceilings (FLOOR-02 コア資産の丸ごと持ち出し). Values are administrator-approved.
#
# ACL decides WHAT a viewer may see; this decides HOW MUCH of it may leave in one call. The
# two are different questions: k=100000 never crosses a permission boundary, yet it moves a
# whole department's corpus into an external AI vendor's context in a single tool call, and
# the ledger keeps no k — so k=5 and k=1000 are indistinguishable after the fact.
#
# `tool` is part of the signature so a later tool (ERN find_expert, k≤5) drops straight in.
_K_CAPS = {
    "lookup_terms": 20,          # 既定値（変更は導入組織の判断で）
    "search_sfaq": 20,           # 〃
    "persons": 10,               # 〃 名簿は最も狭く、break-glass の対象外
    "find_expert": 3,            # ERN。名簿よりさらに狭い (SPEC_phase3 §3)。
                                 #   〃 break-glass の対象外（persons と同じ扱い）
    "sfaq_feedback": 20,         # 〃 1つの問いにぶら下がる判断の本文の上限
    "improvement_tasks": 50,     # 〃 インシデント由来の一覧
    "improvement_requests": 50,  # 〃 製品への要望の一覧
}
# NOTE (R6): ``withdrawn_usage`` is deliberately **absent** from this table, so it lands
# on ``_K_CAP_DEFAULT``. The values above are defaults; changing or adding one is the deploying
# organization's decision and is never raised silently. Falling through to the tightest default
# needs no such decision, and 10 is the right number for
# a list whose own payload says "don't read this unless there's an incident" anyway.
_K_CAP_DEFAULT = 10

# **break-glass を受け付けないツール**（FLOOR-02 の例外の例外）。
#
# 呼び出し側の工夫（`lookup_terms` 側の二重クランプ（`min(...)`）や、「`_clamp_k` を reason
# 無しで呼んでいる」こと）だけに頼ると、それは**引数を書かなかったという事実**に乗った保証に
# なり、他の5ツールと同じ流儀で `reason` を通す改修が入った瞬間に 3 → 30 が黙って開く
# （`_K_OVERRIDE_MULTIPLIER=10`）。30 名をエンジンが並べれば、帯が何であれそれは順位付けであり、
# SPEC_phase3 §3 に直接触れる。
#
# だから呼び出し側ではなく `_clamp_k` の中で締める。ここに載っているツールは、
# 保有 unit の viewer が理由を書いても、監査 sink が生きていても、平常時上限を超えない。
_NO_BREAK_GLASS = frozenset({"persons", "find_expert"})

# 申告本文の写し込みを見るときの、連続一致とみなす長さ。
# 短くしすぎると助詞の並びで誤検知し、長くすると氏名＋連絡先が素通りする。
# 既定値。変更は導入組織の判断で行い、黙って上げない（_K_CAPS と同じ扱い）。
_EVIDENCE_WINDOW = 20
_EVIDENCE_MIN_WINDOW = 4


# WACA Loop 自身への言及を SFAQ 候補から外すための語 (SPEC_improvement_requests §9.2)。
# 完全な判別はできない — これは「うっかり混ぜる」を止める仕掛けであって、意図的な
# 迂回を止める仕掛けではない（evidence_leak と同じ性質）。取りこぼしたぶんは、人が
# 承認の段で落とす（approve_candidate はスチュワード限定）。
_TOOL_SELF_TERMS = (
    "WACA Loop", "waca-loop", "wacaloop",
    "lookup_terms", "search_sfaq", "log_usage", "find_expert",
    "report_improvement_request", "mcpサーバ", "mcp サーバ",
)


def _is_about_the_tool(payload: dict) -> bool:
    """この候補は「組織の判断」ではなく「WACA Loop という道具への注文」か (§9.2)。"""
    text = _flatten(" ".join(str(v) for v in (payload or {}).values()))
    return any(_flatten(t) in text for t in _TOOL_SELF_TERMS)


def _flatten(s: str) -> str:
    """比較用に潰す — NFKC・小文字化・空白を全部除去。"""
    return "".join(unicodedata.normalize("NFKC", s or "").lower().split())


def evidence_leak(evidence: str, text: str | None) -> bool:
    """申告本文 ``evidence`` が ``text`` に写し込まれているか (純粋関数・オフライン検証可)。

    ``evidence in text`` の完全一致では、**改行を1つ挟むだけで抜ける**し、氏名と連絡先だけを
    抜き出して貼る「部分写し」も素通りする — たとえば
    「学び: 山田太郎(yamada@example.org) が会員名簿CSVを貼った件から」が通り、全org
    メンバーが ``search_sfaq`` で読み戻せてしまう。
    そこで、空白と表記ゆれを潰したうえで **evidence 側の連続 ``_EVIDENCE_WINDOW`` 文字が
    どこか1箇所でも text に現れたら写しとみなす**。貼り方の器用さで判定が変わらなくなる。

    **検出できないものを正確に書いておく。** (1) 言い換えられたら分からない。
    (2) **短い申告から氏名だけを抜いた場合も分からない** — 「田中太郎が名簿を貼った」から
    「田中太郎」だけを残されると、窓幅を氏名の長さまで下げない限り当たらず、下げると
    「名簿を貼らない」のような正当な一般化を誤って弾く。氏名そのものを見分けるのは
    重なり判定ではなく固有表現の問題で、この関数の守備範囲の外にある。
    これは人の手抜きを止める仕掛けであって、悪意を止める仕掛けではない — 抽象化するのは
    人である（配達係原則）。**「帰属の漏れを防ぐ」と説明してはならない。**

    **本番の検査経路はここではなく :mod:`waca_loop.improvement.lesson_guard` である。**
    可変窓は正当な言い換えを弾き、かつこの docstring が認めるとおり氏名は捕まえられない
    （あちらは名簿で氏名を見る）。
    この関数は**意図と限界の記録**として、テストとともに残している。
    """
    ev, tx = _flatten(evidence), _flatten(text or "")
    if not ev or not tx:
        return False
    # 短い申告ほど窓を狭める。完全包含に落とすと、**いちばん危ない形**が素通りする —
    # 「田中太郎が名簿を貼った」のような短い申告は氏名と行為しか無く、そこから氏名だけを
    # 抜けば検査を通ってしまう。
    # 下限 _EVIDENCE_MIN_WINDOW は、助詞の並びだけで誤検知しない長さ。
    window = max(_EVIDENCE_MIN_WINDOW, min(_EVIDENCE_WINDOW, len(ev) // 2))
    if len(ev) <= window:
        return ev in tx
    return any(ev[i : i + window] in tx for i in range(len(ev) - window + 1))

# R3b — the retrieval-side observation point. The coverage metric
# `policy_check − usage_recorded` ("confirmed the policy but never logged") cannot see a
# client that reads the org's knowledge all day and calls NEITHER, leaving both counters at 0
# and a mismatch of 0, i.e. looking perfectly compliant while never once entering the
# governance path. Retrieval is the half the client
# cannot skip if it wants answers, so counting it is what makes silence visible.
#
# Content-free by design (the tool name only, never the query) — same discipline as the rest
# of tool_events. One thin row per read call.
RETRIEVAL_EVENT = "retrieval"
# The break-glass raises the ceiling; it does not remove it. Without this, an export admin
# whose client was talked into a plausible-sounding reason could ship the whole corpus in one
# call — the exact thing FLOOR-02 forbids, re-opened for the population with the most to
# export. 10× is enough for a real audit sweep and still bounded.
_K_OVERRIDE_MULTIPLIER = 10


class _PersonSearch(Protocol):
    def search(self, query: str, limit: int, viewer: Any | None = None) -> list[Any]:
        ...

    # 氏名ゲート用（lesson_guard.names_in）。プロトコルに宣言しておかないと、双子の
    # 片方に生やし忘れても型検査に当たらず、オフラインテストは双子側しか踏まないので
    # 永久に緑のまま本番だけ AttributeError で落ちる。
    def roster_names(self) -> tuple[str, ...]:
        ...


class _UsageStore(Protocol):
    """The ledger surface both twins must implement.

    Declares every method, not ``record`` alone: a twin that implemented only one side of a
    new method would otherwise be invisible to type checking.
    """

    def record(
        self, record: UsageRecord, *, occurred_at: Any | None = ..., dedupe: bool = ...
    ) -> str:
        ...

    def existing_log_id(
        self, record: UsageRecord, *, now: Any | None = ...
    ) -> str | None:
        """Resend de-duplication. Declared here, not probed with ``getattr``.

        A ``getattr`` gate is "open" exactly where the store is unusual — the failure mode
        CLAUDE.md warns about for capability-shaped gates. Declaring it means a twin that
        implements only one side is a type error, not a silently disabled de-duplication.
        """
        ...

    def distinct_actors(self, window_days: int) -> set[str]:
        ...

    def set_status(self, log_id: str, status: str) -> bool:
        ...

    def delete(self, log_id: str) -> bool:
        ...

    def withdraw(
        self,
        log_id: str,
        *,
        actor_id: str | None,
        actor_raw: str | None,
        subject_hash: str | None = ...,
        reason: str,
        at: Any | None = ...,
    ) -> bool:
        ...

    def list_withdrawn(self, limit: int) -> list[dict]:
        ...

    def get_withdrawn(self, log_id: str) -> dict | None:
        ...


def _contains_evidence(evidence: str, text: str) -> bool:
    """申告の本文が `text` に写されているか（純ロジック・オフラインで試験できる）。

    生の部分一致では、句点のあとに空白を1つ足す・改行を挟む・一文字消す、のいずれでも
    抜けてしまう。急いで貼った人を止めるための
    検査なので、**急いで少しだけ触った形**を通してしまうと意味が無い。

    そこで空白を落として比べ、さらに近似一致（difflib）も見る。閾値 0.9 は「ほぼ写し」で、
    人が自分の言葉で書き直した文はここまで似ない。厳しすぎた場合に起きるのは
    「書き直してください」と言われることだけなので、緩いより厳しい側に倒している。
    """
    import difflib

    ev = "".join((evidence or "").split())
    tx = "".join((text or "").split())
    if not ev or not tx:
        return False
    if ev in tx:
        return True
    # 申告が長く学びが短い場合に効くよう、短いほうを窓にして比べる
    return difflib.SequenceMatcher(None, ev, tx).ratio() >= 0.9


@dataclass
class ServerContext:
    """Everything a request needs. Built once per authenticated session (§5)."""

    term_store: TermStore
    usage_store: _UsageStore
    embedder: Embedder
    viewer: Viewer
    person_store: _PersonSearch | None = None
    # Survey / improvement loop. Optional — tools below need them.
    survey_store: Any | None = None
    task_store: Any | None = None
    request_store: Any | None = None      # 改善要望 (SPEC_improvement_requests)
    # 要望をシートへ即時反映する呼び出し可能（§10・best-effort）。未配線なら何もしない。
    request_sheet_mirror: Any | None = None
    candidate_store: Any | None = None
    policy_store: Any | None = None       # AI policy rows, level制 (SPEC_phase1 §9)
    policy_guide_store: Any | None = None  # Lite Docs-derived policy guides
    adoption_map_store: Any | None = None  # Lite Sheets-derived adoption map
    knowledge_store: Any | None = None    # SFAQ questions/feedback (SPEC_phase2)
    attribution: str = "pseudonymous"     # pseudonymous | identified | anonymous
    cadence: str = "monthly"
    # WACA Loop ops guards (SPEC_ops P0). Optional so existing tests/wiring stay untouched.
    scope_registry: Any | None = None     # SessionScopeRegistry (R1, process-wide)
    tool_event_store: Any | None = None   # tool_events trail (R3a, best-effort)
    org_guard_strict: bool = False        # R2: absent org_context also goes pending
    # Units allowed to exceed the per-tool result cap, with a reason (FLOOR-02 break-glass).
    # Empty = nobody, which is the fail-closed default until docs/ACCESS_MATRIX.md settles it.
    export_admin_units: frozenset[str] = frozenset()
    # ERN (SPEC_phase3). Optional — find_expert returns empty without them.
    ern_assertion_store: Any | None = None   # ern_assertions（宣言/導出プロファイル）
    ern_audit_store: Any | None = None       # ern_audit（append-only。読み出しツールは作らない）
    ern_half_life_days: int = 180            # E9: 実値は config 由来。ここは既定の写し
    ern_role_fallback: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # --- WACA Loop ops guard helpers (SPEC_ops P0) ---
    def _trail(self, tool: str, detail: str | None = None) -> None:
        """Best-effort R3a event; a trail failure must never break the tool call."""
        if self.tool_event_store is None:
            return
        try:
            from waca_loop.auth.identity import ledger_actor

            actor_id, actor_raw = ledger_actor(self.viewer)
            self.tool_event_store.record(
                tool, actor_id=actor_id, actor_raw=actor_raw, detail=detail
            )
        except Exception as exc:  # noqa: BLE001 — observability only, tools must stay up
            # Still best-effort, but never silent. A silent trail outage would look exactly
            # like a client that never called the tool, which is the one thing the coverage
            # story must be able to tell apart. The class name only — an exception body can
            # echo the row (actor, detail) into the log.
            logger.warning(
                "waca-loop-trail: %s failed: %s", tool, exc.__class__.__name__
            )

    def _trail_required(self, tool: str, detail: str | None = None) -> None:
        """R3a event that MUST land before an irreversible act — the one exception to R3a.

        ``_trail`` is best-effort by design (observability must not break tools). ``discard``
        is different: it physically removes the ledger row, so a lost event means the deletion
        leaves no trace anywhere in the system. A *configured* trail store that fails therefore
        aborts the operation. With no store wired at all (stdio/tests) this is a no-op, exactly
        like ``_trail`` — hosted mode always wires one (``build_shared_runtime``).
        """
        if self.tool_event_store is None:
            return
        from waca_loop.auth.identity import ledger_actor

        actor_id, actor_raw = ledger_actor(self.viewer)
        self.tool_event_store.record(tool, actor_id=actor_id, actor_raw=actor_raw, detail=detail)

    def _coverage_hint(self) -> str | None:
        """C4 nudge: one line about **this caller's own** unrecorded turns (SPEC_coverage §3 C4).

        Two constraints define this method, and both are load-bearing:

        1. **Self only, checked twice.** The actor comes from ``ledger_actor(self.viewer)`` —
           the same attribution the ledger uses (§5) — and is handed to a query that pins
           itself to a single actor; the returned rows are then re-filtered here on the same
           predicate. That redundancy is §3.3's rule applied to the trail: the store's gate
           keeps other actors' rows inside BigQuery, and this one keeps them out of the hint
           if that gate ever regresses. No argument reaches either gate from the client. A
           hint is a mild thing, but "who is not logging their AI use" is exactly the kind of
           fact that must not travel sideways between colleagues.
        2. **Advice, never a gate.** Best-effort like ``_trail``: any failure — no store, a
           store without the query, a broken backend — yields ``None`` and the policy body is
           served regardless (§9.4 の流儀). The nudge is worth having only while it cannot
           cost anyone their policy.
        """
        store = self.tool_event_store
        if store is None or not hasattr(store, "recent_by_actor"):
            return None
        # ★ A session declared external/private is SUPPOSED to have no ledger rows: R1 refuses
        # the write, so `usage_recorded` never lands while `policy_check` still does. Left
        # alone, the nudge would tell someone who declared correctly that they keep forgetting
        # to log — and the cheapest way for a model to make that stop is to log the very
        # 他組織・私事 content the operating rule 2 (SPEC_ops §6) and R1 exist to keep out of
        # the ledger. A nudge that pressures people across a boundary is worse than no nudge.
        if self._write_refusal() is not None:
            return None
        try:
            from waca_loop.auth.identity import ledger_actor
            from waca_loop.ledger.tool_events import coverage_hint

            actor_id, actor_raw = ledger_actor(self.viewer)
            if actor_id is None and actor_raw is None:
                return None
            rows = store.recent_by_actor(actor_id=actor_id, actor_raw=actor_raw)
            # §3.3 の二重化をここでも守る。取得側の述語（SQL / in-process）が一段目、これが
            # 二段目。片方だけなら、片方が壊れた日に他人の記録状況がヒントに混ざる。
            from waca_loop.ledger.tool_events import same_actor

            rows = [r for r in rows if same_actor(r, actor_id, actor_raw)]
            return coverage_hint(rows)
        except Exception:  # noqa: BLE001 — advisory only; the policy must still be served
            return None

    def _scope_is_lapsed(self) -> bool:
        """`is_lapsed` for a registry that may not have it (SPEC_ops R1d).

        `scope_registry` is duck-typed (`Any | None`, and `getattr(..., "ttl_hours", 0)`
        downstream), so a registry without `is_lapsed` is an anticipated shape. Without this
        the two halves would disagree under one fault: `_standing_scope_notice` swallows and
        goes silent while `_write_refusal` has no try and raises — one hiding the block, the
        other killing the call. Missing method → False → standing-only behavior, which is a
        degradation and not a surprise.
        """
        reg = self.scope_registry
        probe = getattr(reg, "is_lapsed", None)
        if probe is None:
            return False
        try:
            actor_id, actor_raw = self._scope_actor()
            return bool(probe(actor_id=actor_id, actor_raw=actor_raw))
        except Exception:  # noqa: BLE001 — never let a predicate decide a tool call by raising
            return False

    def _standing_scope_notice(self) -> str | None:
        """R1c: one line when this caller's writes are excluded by a standing declaration.

        Same two constraints as :meth:`_coverage_hint` — **self only** (the scope comes from
        this viewer's own subject; no argument reaches the lookup) and **advisory** (any
        failure yields ``None`` and the policy is served regardless).

        Why here and not only at the refusal: the refusal fires on the first *write*, and
        every turn before someone actually reads it is silently unrecorded. Saying it where
        the turn already starts turns a multi-session outage into one sentence.
        """
        try:
            from waca_loop.guard import (
                EXCLUDED_SCOPES,
                lapsed_refusal_message,
                write_refusal_message,
            )

            if self.scope_registry is None:
                return None
            actor_id, actor_raw = self._scope_actor()
            declared_at = self.scope_registry.declared_at(
                actor_id=actor_id, actor_raw=actor_raw
            )
            # R1d: writes stay refused after a lapse, so the notice has to survive it too.
            # Keying this on `scope_of` alone would hide the explanation at exactly the moment
            # it is most needed — the reader's declaration expired, writes are still refused,
            # and nothing on screen says why (SPEC_ops R1d).
            if self._scope_is_lapsed():
                return lapsed_refusal_message(
                    self.scope_registry.stored_scope(actor_id=actor_id, actor_raw=actor_raw),
                    declared_at,
                )
            scope = self.scope_registry.scope_of(actor_id=actor_id, actor_raw=actor_raw)
            if scope not in EXCLUDED_SCOPES:
                return None
            return write_refusal_message(
                scope,
                declared_at,
                self.scope_registry.expires_in_hours(actor_id=actor_id, actor_raw=actor_raw),
            )
        except Exception:  # noqa: BLE001 — advisory only; the policy must still be served
            return None

    def _scope_expiry_warning(self) -> str | None:
        """Warn shortly before an exclusion lapses (SPEC_ops R1).

        Under SPEC_ops R1d the lapse does not resume anything, so this is a
        **renewal** prompt, not a data-loss one: after it fires, writes stay refused until the
        actor declares again. Saying so before the window closes is what keeps the block from
        arriving unexplained — the reader can renew now instead of discovering the refusal
        later.

        Self-scoped and advisory like the rest of §9.4 — never blocks, ``None`` on any failure.
        """
        try:
            from waca_loop.guard import EXCLUDED_SCOPES, SCOPE_EXPIRY_WARNING_HOURS

            if self.scope_registry is None:
                return None
            actor_id, actor_raw = self._scope_actor()
            if self.scope_registry.scope_of(
                actor_id=actor_id, actor_raw=actor_raw
            ) not in EXCLUDED_SCOPES:
                return None
            left = self.scope_registry.expires_in_hours(
                actor_id=actor_id, actor_raw=actor_raw
            )
            if left is None or left > SCOPE_EXPIRY_WARNING_HOURS or left <= 0:
                return None
            return (
                f"業務外の宣言があと約{max(1, int(left))}時間で自動失効します。"
                "失効しても記録は自動では再開しませんが、失効後は"
                "set_session_scope で宣言し直すまで記録が止まったままになります。"
            )
        except Exception:  # noqa: BLE001 — advisory only
            return None

    def _scope_lapse_notice(self) -> str | None:
        """Once, right after an exclusion lapses: say that it lapsed and recording has NOT resumed.

        The pre-expiry warning can be missed entirely — asleep through the window, or a client
        that never calls ``get_applicable_policy``. Under SPEC_ops R1d what waits on the
        other side is not recording but a **block that has outlived its declaration**, and one
        that does not expire. A block nobody was told about is indistinguishable from a
        bug, so the lapse announces itself.

        Advisory and self-scoped like its neighbours; fires once per lapse, not every turn.
        """
        try:
            if self.scope_registry is None:
                return None
            actor_id, actor_raw = self._scope_actor()
            was = self.scope_registry.take_lapse_notice(
                actor_id=actor_id, actor_raw=actor_raw
            )
            if not was:
                return None
            return (
                f"業務外の宣言（{was}）が自動失効しました。"
                "**記録は再開していません** — 再開には明示の宣言が要ります。"
                "組織の業務に戻ったなら set_session_scope に scope=\"org\" を、"
                "まだ業務外なら同じ scope を渡して宣言し直してください。"
            )
        except Exception:  # noqa: BLE001 — advisory only
            return None

    def _scope_vanished_notice(self) -> str | None:
        """Once, when a blocking declaration disappeared from the store (SPEC_ops R1d).

        `session_scopes` is `PARTITION BY DATE(declared_at)` with `partition_expiration_days
        = 90`, so a declaration nobody renewed is dropped on day 91 and the block lifts. That
        is a **second, slower resumption path** — exactly the 「本人の操作なしに台帳へ入る」
        shape R1d exists to prevent, delayed by ninety days rather than removed.

        Retention is deliberately unchanged: the row reads as a label stuck on a person, and
        keeping it forever to enforce a block has its own cost. Instead, the disappearance is
        not silent. Advisory and self-scoped like its neighbours.
        """
        try:
            if self.scope_registry is None:
                return None
            probe = getattr(self.scope_registry, "take_vanished_notice", None)
            if probe is None:
                return None
            actor_id, actor_raw = self._scope_actor()
            was = probe(actor_id=actor_id, actor_raw=actor_raw)
            if not was:
                return None
            return (
                f"業務外の宣言（{was}）の記録が保持期間（90日）を過ぎて消えました。"
                "**これにより記録が再開されています。**まだ業務外なら "
                "set_session_scope で宣言し直してください。"
            )
        except Exception:  # noqa: BLE001 — advisory only
            return None

    def _clamp_k(self, k: int | None, tool: str, reason: str | None = None) -> int:
        """Bound how much a single call may extract (FLOOR-02). Never raises — it clamps.

        Everyday ceiling: ``_K_CAPS[tool]``. A viewer in ``export_admin_units`` may exceed it
        **only** by stating a reason AND only when an audit sink exists to record that they
        did — a break-glass with no trail is not a break-glass. Tools in
        ``_NO_BREAK_GLASS`` (the roster and ERN) cannot be exceeded at all. Every other case
        is clamped silently: refusing the call would push callers to paginate around the cap, which
        defeats the purpose and produces no audit trail either.

        Clamping, not raising, is also why this is advisory-shaped like the rest of §9: the
        tool still answers, it just answers with the everyday amount.
        """
        cap = _K_CAPS.get(tool, _K_CAP_DEFAULT)
        try:
            k = int(k)
        except (TypeError, ValueError):
            return cap
        if k < 1:
            return 1
        if k <= cap:
            return k
        if tool in _NO_BREAK_GLASS:
            return cap   # 名簿系は超過そのものを受け付けない（理由も権限も関係ない）
        if not (self.viewer.units & self.export_admin_units):
            return cap
        if not (reason or "").strip():
            return cap
        if self.tool_event_store is None:
            return cap   # 監査行が残せないなら超過も許さない
        k = min(k, cap * _K_OVERRIDE_MULTIPLIER)   # 超過にも天井を置く
        # Recorded synchronously and WITHOUT the best-effort swallow `_trail` applies: for
        # everything else a lost trail row costs observability, here it would mean the
        # override happened unrecorded. A streaming-insert quota trip or a transient 503 is
        # enough to reach that, so a failed write demotes the call to the everyday cap.
        # Content-free, exactly like set_session_scope's reason: the row records THAT a
        # break-glass happened and how big, never the caller's prose.
        try:
            from waca_loop.auth.identity import ledger_actor

            actor_id, actor_raw = ledger_actor(self.viewer)
            self.tool_event_store.record(
                "k_override", actor_id=actor_id, actor_raw=actor_raw,
                detail=f"{tool}:k={k}:cap={cap}",
            )
        except Exception:  # noqa: BLE001 — no audit row, no override
            return cap
        return k

    # Where an incident goes when its home unit is ambiguous. Not org (everyone) and not
    # None (nobody, which loses the report): the stewards are the governance group whose job
    # this already is, and it is the narrowest audience that can still act.
    _SURVEY_FALLBACK_UNIT = STEWARD_UNIT

    def _survey_owner_unit(self, requested: str | None = None, escalate: bool = False) -> str:
        """Owning unit for a survey response / its improvement tasks (§3.3). Never guesses.

        Unlike :func:`resolve_owner_unit`, this never raises: refusing to accept an incident
        report because the reporter belongs to two departments would be the wrong trade — the
        report is the thing we must not lose. One owning unit → use it. Ambiguous, absent, or
        a unit the reporter does not belong to → route to the stewards, who can then place it.

        ``escalate=True`` sends it past the reporter's own department to the stewards. This is
        the conflict-of-interest escape: the default destination is the reporter's own unit,
        which in a personnel matter frequently contains the person being reported. The
        reporter has to be able to route around their own chain without being told to file it
        with them. (If the concern is about the stewards themselves, no in-system destination
        is right — the tool description says so and points at a human channel.)
        """
        from waca_loop.retrieval.acl import owning_units

        if escalate:
            return self._SURVEY_FALLBACK_UNIT
        units = owning_units(self.viewer)
        if requested is not None and requested in units:
            return requested
        if requested is None and len(units) == 1:
            return next(iter(units))
        return self._SURVEY_FALLBACK_UNIT

    def _require_identity(self, action: str) -> None:
        """Refuse shared-layer writes from a viewer whose identity never resolved (§5).

        Anything written here is served back to other people — the org's vocabulary, the SFAQ
        judgment layer. Attribution is not optional for those. The hosted wiring's
        `person_id is None` check in ``build_request_context_factory`` alone would be one gate,
        on one code path (片肺), between an unresolved subject and `propose_candidates →
        approve_candidate → 全員に配信`. This is the second half: the same refusal on the context
        itself, so the stdio path and any direct
        ``ServerContext`` construction fail closed too.

        ``log_usage`` deliberately does NOT go through here: the ledger is designed to record
        unresolved actors as a hashed ``actor_raw`` (§5), and dropping their usage would blind
        the very monitor that is supposed to see them. ``set_session_scope`` /
        ``review_pending_usage`` / ``submit_survey_response`` are likewise exempt — they write
        to the caller's own session or the personal survey layer, not to a layer served back
        to other members. (submit_survey_response IS read back via improvement tasks, which
        carry their own owner-unit gate.)
        """
        if not self.viewer.person_id:
            raise PermissionError(
                f"cannot {action}: the caller's identity did not resolve to a person "
                "(§5). Shared-layer writes must be attributable."
            )

    def _scope_actor(self) -> tuple[str | None, str | None]:
        """This viewer's §5 identity pair — the key every scope lookup uses.

        One helper rather than five inline `ledger_actor` calls: the declaration is
        persisted, and a call site that derived the key differently would read or write
        somebody else's row.
        """
        from waca_loop.auth.identity import ledger_actor

        return ledger_actor(self.viewer)

    def _session_scope(self) -> str:
        """This subject's declared scope, or ``unknown`` when nothing was declared (R1/R1b)."""
        if self.scope_registry is None:
            return "unknown"
        actor_id, actor_raw = self._scope_actor()
        return self.scope_registry.scope_of(actor_id=actor_id, actor_raw=actor_raw)

    def _write_refusal(self) -> dict | None:
        """R1: refuse WACA Loop writes for a subject standing at external/private (no-op + why).

        The refusal carries the **standing scope** and the way out, because the declaration
        outlives the session that made it: it is keyed by the actor (§5) in a registry that
        persists, so a scope declared for one conversation suppresses every later one until it
        is re-declared **or the TTL lapses**. That is fail-closed by design and stays that way —
        but a refusal nobody can diagnose is a different fault (writes refused across sessions,
        cause invisible from the message). So the refusal names the scope, its age, and how long it
        has left.
        """
        from waca_loop.guard import lapsed_refusal_message, write_refusal_message

        actor_id, actor_raw = (None, None)
        if self.scope_registry is not None:
            actor_id, actor_raw = self._scope_actor()
        if self.scope_registry is not None and self.scope_registry.writes_blocked(
            actor_id=actor_id, actor_raw=actor_raw
        ):
            declared_at = self.scope_registry.declared_at(
                actor_id=actor_id, actor_raw=actor_raw
            )
            lapsed = self._scope_is_lapsed()
            # R1d: report the scope the actor actually declared, not the `unknown` that expiry
            # turns it into. "scope: unknown なのに拒否された" is the shape of a bug report.
            scope = (
                self.scope_registry.stored_scope(actor_id=actor_id, actor_raw=actor_raw)
                if lapsed
                else self.scope_registry.scope_of(actor_id=actor_id, actor_raw=actor_raw)
            )
            if lapsed:
                reason = lapsed_refusal_message(scope, declared_at)
            else:
                reason = write_refusal_message(
                    scope,
                    declared_at,
                    self.scope_registry.expires_in_hours(
                        actor_id=actor_id, actor_raw=actor_raw
                    ),
                )
            return {
                "recorded": False,
                "refused": True,
                # A client can branch on this without reading the prose. **Not** named
                # `scope_lapsed`: `get_applicable_policy` already returns that name carrying
                # the Japanese one-shot notice, and one name meaning a bool on ten write tools
                # and a sentence on another is how a client ends up rendering `True` to a user.
                "scope_state": "lapsed" if lapsed else "standing",
                # The scope is echoed as its own field, not only inside the prose: a client
                # can act on a field without parsing a sentence. (Not the coverage report —
                # that reads tool_events from BigQuery and never sees a return value.)
                "scope": scope,
                "reason": reason,
            }
        return None

    # TOOL set_session_scope — R1: declare this session's org affiliation.
    def set_session_scope(self, scope: str, reason_summary: str | None = None) -> dict:
        """Declare org|external|private|unknown for the caller's session (SPEC_ops R1).

        external/private block all WACA Loop writes from this subject until re-declared. The
        ``reason_summary`` is intentionally NOT persisted — exclusion metrics keep counts
        only (no content, no org names); it exists so the model states its judgment.
        """
        from waca_loop.guard import (
            DEFAULT_SCOPE_TTL_HOURS,
            EXCLUDED_SCOPES,
            SessionScopeRegistry,
        )

        if self.scope_registry is None:
            # 設定があれば見る。SCOPE_TTL_HOURS=0（自動失効なし）がこの経路だけ効かない
            # 状態にしないため。ただし **必須設定が
            # 無い環境でも落とさない** — オフラインのスイートはここを通り、`get_settings()`
            # は GCP_PROJECT 等が無いと送出する（INV: テストは資格情報なしで通ること）。
            ttl = DEFAULT_SCOPE_TTL_HOURS
            try:
                from waca_loop.config import get_settings

                ttl = get_settings().scope_ttl_hours
            except Exception:  # noqa: BLE001 — 設定が無い環境では既定でよい
                pass
            self.scope_registry = SessionScopeRegistry(ttl_hours=ttl)
        actor_id, actor_raw = self._scope_actor()
        declared = self.scope_registry.declare(
            actor_id=actor_id, actor_raw=actor_raw, scope=scope
        )
        self._trail("scope_declared", detail=declared)
        if declared in EXCLUDED_SCOPES:
            # 宣言はこの会話で終わらない。**作る側でも伝える** — 拒否文だけで伝えると、
            # 宣言した本人は「この会話の話」と思ったまま次の会話へ行き、そこで初めて（拒否文
            # を読んで）常駐に気づくことになる。作った時点で言うほうが早い。
            ttl = getattr(self.scope_registry, "ttl_hours", 0)
            lifetime = (
                f"**この宣言は約{ttl}時間で自動失効します** — ただし失効しても記録は"
                "自動では再開しません。失効後は、組織の業務なら scope=\"org\" を、"
                "まだ業務外なら同じ scope を、もう一度宣言してください。"
                if ttl and ttl > 0
                else "**この宣言はこの会話では終わりません** — 解除するまで残ります。"
            )
            # 寿命は**宣言した時点で**言う (SPEC_ops R1)。あとから拒否文で知らせる形にすると、
            # 決めたときの理解が「この会話の話」のままになる。TTL があるときに「解除するまで」と
            # 書くと嘘になるので、TTL の有無で文面を分ける。
            message = (
                "以降 waca-loop への記録は行いません。この宣言はこの会話では終わりません — "
                f"同じ利用者の後続のセッションでも記録は拒否されます。{lifetime}"
                "組織の業務に戻ったら set_session_scope に scope=\"org\" を渡してください。"
            )
        else:
            # 常駐の告知は**両方の向きに**要る。`org` も同じだけ sticky で、こちらは fail-OPEN
            # 側に効く（宣言が立っている間は「この人は 組織の業務中」と見なされる）。片側だけ
            # 警告すると、拒否を解除するために org を宣言した人が、それが後続の会話にも残る
            # ことを知らないまま次の私事の会話に入る。
            message = (
                "組織の業務として記録します。**この宣言もこの会話では終わりません** — "
                "解除するまで後続のセッションにも残ります。業務外の会話に移るときは "
                "set_session_scope に external/private を宣言してください。"
            )
        return {"scope": declared, "message": message}

    # TOOL review_pending_usage — R2: steward approves/discards org-unconfirmed rows.
    def review_pending_usage(self, log_id: str, action: str) -> dict:
        """Steward-only (§2.4 流儀): approve → recorded / discard → physical delete.

        ``discard`` is irreversible — the row leaves ``usage_log`` and nothing else in the
        system remembers it existed — so the *act* is trailed to ``tool_events`` **before**
        the mutation, and fail-closed: if a configured trail store cannot record it, the row
        is not deleted (SPEC_ops R3a 例外). Recording an attempt
        that later turns out to be a bad ``log_id`` is far cheaper than a silent deletion with
        no record anywhere. The event carries the action and the ``log_id`` only, never the
        discarded summary (R3a: 内容は持たない).
        """
        if STEWARD_UNIT not in self.viewer.units:
            raise PermissionError("only the steward may review pending usage (SPEC_ops R2)")
        if action not in ("approve", "discard"):
            raise ValueError("action must be approve|discard")
        # detail stays a closed shape: "<action>:<log_id>". log_id is caller-supplied, and a
        # length cap alone is not a shape — 64 characters of prose ("田中さんの給与相談の記録")
        # would land verbatim in a table whose DDL says 内容は一切持たない, and
        # coverage_report prints it to an operator console. Ledger ids are UUIDs
        # (`new_log_id`), so require that: a non-id argument is refused BEFORE anything is
        # written, which keeps the "record the attempt" property for every real id.
        import uuid

        try:
            uuid.UUID(str(log_id))
        except (ValueError, AttributeError, TypeError):
            raise ValueError(
                "log_id は usage_log の UUID を渡すこと（説明文や検索語は受け付けない）"
            ) from None
        detail = f"{action}:{log_id}"
        if action == "discard":
            self._trail_required("usage_reviewed", detail=detail)   # must land before deleting
        else:
            self._trail("usage_reviewed", detail=detail)            # R3a best-effort
        if action == "approve":
            ok = self.usage_store.set_status(log_id, "recorded")
        else:
            ok = self.usage_store.delete(log_id)
        if not ok:
            raise ValueError(f"usage row not found: {log_id}")
        return {"log_id": log_id, "action": action, "done": True}

    # TOOL request_log_deletion — R6: the owner withdraws their own ledger row.
    def request_log_deletion(self, log_id: str, reason: str) -> dict:
        """本人申告 (R6): hide the caller's own row from every ordinary read, at once.

        **Immediately, and that word is load-bearing.** The withdrawal is an append to
        ``usage_withdrawals``, not an edit of the ledger row, because BigQuery refuses DML
        against rows still in the streaming buffer — and the row someone wants withdrawn is
        almost always one written minutes ago. See ``ledger/usage_log`` for the full note.

        Ownership is enforced inside the statement that writes the withdrawal, so another
        person's ``log_id`` cannot be withdrawn — and "not yours" and "no such row" answer
        identically, so the tool can't be used to probe whose ids exist.

        **The trail records the outcome, not the intent.** Trailing before the store call would
        leave a refused attempt as an event asserting a withdrawal that never happened. Both
        outcomes are trailed, with different tool names, so a brute-force sweep across other
        people's ids is visible as a
        run of ``usage_withdraw_refused`` — and ``coverage_report`` surfaces it. Unlike
        ``discard`` this is best-effort: withdrawal destroys nothing, so making it fail-closed
        would only mean an observability outage keeps someone's private content visible.

        ``reason`` is a closed code, never prose: see :data:`DELETION_REASONS`.
        """
        import uuid

        from waca_loop.auth.identity import hash_subject, ledger_actor
        from waca_loop.ledger.usage_log import DELETION_REASONS

        if reason not in DELETION_REASONS:
            raise ValueError(
                "reason must be one of " + "|".join(DELETION_REASONS)
                + "（自由文は受け付けない — 取り下げたい内容をここに書き写さないため）"
            )
        try:
            # Canonicalise, don't just validate: uuid.UUID accepts `urn:uuid:…`, braces and
            # stray dashes, and the raw string would otherwise reach tool_events.detail —
            # a table whose DDL says 内容は一切持たない.
            log_id = str(uuid.UUID(str(log_id)))
        except (ValueError, AttributeError, TypeError):
            raise ValueError(
                "log_id は usage_log の UUID を渡すこと（説明文や検索語は受け付けない）"
            ) from None
        actor_id, actor_raw = ledger_actor(self.viewer)
        # Rows written before this person's sub was bound to a person_id carry actor_raw only.
        # `Viewer.anonymous` already stores a hashed subject; a resolved viewer
        # carries the raw one, so only that case gets hashed here.
        subject_hash = (
            hash_subject(self.viewer.subject) if self.viewer.person_id else self.viewer.subject
        )
        ok = self.usage_store.withdraw(
            log_id,
            actor_id=actor_id,
            actor_raw=actor_raw,
            subject_hash=subject_hash,
            reason=reason,
        )
        self._trail(
            "usage_withdrawn" if ok else "usage_withdraw_refused", detail=f"{reason}:{log_id}"
        )
        if not ok:
            raise ValueError(f"usage row not found: {log_id}")
        return {
            "log_id": log_id,
            "reason": reason,
            "withdrawn": True,
            "message": (
                "この記録は取り下げました。通常の読み取り・集計からは外れます。"
                "記録自体は消していないため、インシデント調査が必要な場合に限り"
                "スチュワードが警告つきで参照できます。"
            ),
        }

    _WITHDRAWN_WARNING = (
        "これは論理削除済みです。インシデントなど必要がなければ閲覧しないでください。"
    )

    # TOOL list_withdrawn_usage — R6: steward triages withdrawn rows, warned.
    def list_withdrawn_usage(self, limit: int | None = None) -> dict:
        """Steward-only. **Identifies** withdrawn rows; it does not hand back their content.

        ``summary`` is absent by design. This set is, by construction, the summaries people
        looked at and decided were too private to keep, so a list tool that ships the newest
        ones into an external model's context on demand is the wrong shape.
        ``log_id``/``occurred_at``/``actor_id``/``use_category``/``reason`` identify
        and triage; :meth:`get_withdrawn_usage` reads one row's content and is trailed
        fail-closed with that id, so "which private summary did the steward read" has an
        answer afterwards.

        The warning is part of the payload, returned on every call including the empty one.
        """
        if STEWARD_UNIT not in self.viewer.units:
            raise PermissionError("only the steward may read withdrawn usage rows (R6)")
        k = self._clamp_k(limit, "withdrawn_usage")
        self._trail("withdrawn_usage_listed", detail=str(k))
        return {
            "warning": self._WITHDRAWN_WARNING,
            "rows": self.usage_store.list_withdrawn(k),
        }

    # TOOL get_withdrawn_usage — R6: the investigation step, one row, always trailed.
    def get_withdrawn_usage(self, log_id: str, reason: str) -> dict:
        """Steward-only. Returns ONE withdrawn row **with** its summary, and says who read it.

        Separated from the list on purpose. Reading content the owner asked to
        hide is the rare incident step, so it costs a stated ``reason`` and leaves a
        fail-closed audit row naming the ``log_id`` — the same shape ``discard`` uses. If the
        trail cannot record the read, the read does not happen: unlike a withdrawal, nothing
        here becomes *more* private by failing.
        """
        import uuid

        if STEWARD_UNIT not in self.viewer.units:
            raise PermissionError("only the steward may read withdrawn usage rows (R6)")
        if not (reason or "").strip():
            raise ValueError("reason は必須（何のために読むのかを残す — R6）")
        try:
            log_id = str(uuid.UUID(str(log_id)))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("log_id は usage_log の UUID を渡すこと") from None
        # detail keeps a closed shape: the id only. The steward's stated reason is free text
        # and does NOT go into tool_events (内容は一切持たない).
        self._trail_required("withdrawn_usage_read", detail=log_id)
        row = self.usage_store.get_withdrawn(log_id)
        if row is None:
            raise ValueError(f"withdrawn usage row not found: {log_id}")
        return {"warning": self._WITHDRAWN_WARNING, "row": row}

    # TOOL get_pii_anonymization_guidance — 開発者の pii-anonymization-advisor スキルのMCP移植.
    def get_pii_anonymization_guidance(
        self, columns: list[str], purpose: str | None = None
    ) -> dict:
        """PII detection + 匿名化仕様書 from column NAMES only (values never sent).

        Two-step, mirroring the skill: without ``purpose`` → detection + the stop message +
        purpose options (A–E) to ask the user; with ``purpose`` → the full spec table.
        Advice only — the server never blocks the *user's work* (INV-6); execution is out of
        scope. It does, however, refuse **its own** input when actual values were passed
        instead of column names: INV-6 is about not vetoing a judgment call, not about
        accepting raw PII (FLOOR-01).
        """
        from waca_loop.pii import (
            PURPOSES,
            STOP_MESSAGE,
            VALUE_REJECTION_MESSAGE,
            anonymization_spec,
            detect_pii_columns,
            value_like_columns,
        )

        # Server-side half of the two-line defense: the tool description says 列名のみ, this
        # catches the case where a value slipped through anyway. The offending strings are
        # never echoed back — only their positions and shapes.
        offenders = value_like_columns(columns)
        if offenders:
            detail = "、".join(f"{o['index'] + 1}番目: {o['reason']}" for o in offenders)
            raise ValueError(VALUE_REJECTION_MESSAGE.format(detail=detail))

        detected = detect_pii_columns(columns)
        if not detected:
            return {
                "pii_detected": False,
                "message": "個人情報らしき項目は見つかりませんでした（列名判定）。"
                           "通常どおり処理できます。",
            }
        if purpose is None:
            return {
                "pii_detected": True,
                "detected": detected,
                "stop": True,
                "message": STOP_MESSAGE,
                "purposes": PURPOSES,
                "next": "利用目的（A〜E)をユーザーに確認し、purpose を指定して再度呼ぶこと。",
            }
        out = anonymization_spec(columns, purpose)
        out["pii_detected"] = True
        out["handoff"] = (
            "この仕様に沿って元データを匿名化する処理を実行してください（GAS・スクリプト・"
            "手作業など任意）。匿名化済みデータの再添付後に当初の依頼を実行します。"
        )
        return out

    # --- TOOL lookup_terms (§4.2) ---
    def lookup_terms(self, query: str, k: int = 5, reason: str | None = None) -> dict:
        # The roster is the tightest cap AND the only PII corpus of the three, so it is not
        # break-glass-able: a reason given to export vocabulary must not also authorize a
        # roster dump. Note person_k is derived from the ORIGINAL k, not the clamped one —
        # feeding the clamped value in would let an override slip past the persons cap.
        person_k = min(self._clamp_k(k, "persons"), _K_CAPS["persons"])
        k = self._clamp_k(k, "lookup_terms", reason)          # FLOOR-02
        hits = hybrid_search(
            query, self.viewer, k, store=self.term_store, embedder=self.embedder
        )
        person_count = 0
        if self.person_store is not None and self.viewer.is_org_member:
            from waca_loop.retrieval.acl import person_visible_to

            # The roster is PII, so it runs the same double gate as everything else: the
            # store gates it (Gate 1, SQL in production) and this re-checks in-process
            # (Gate 2, §3.3). Hard-coding the scope tags without consulting a gate would let an
            # opt-out or a frozen row surface.
            #
            # Resolution itself is org-wide — naming a
            # colleague is what this layer is for. The bulk-enumeration risk is bounded by
            # person_k (FLOOR-02), not by narrowing who may be named. Hence ORG tags: they
            # describe the real visibility, so a downstream re-filter agrees.
            for person in self.person_store.search(query, person_k, self.viewer):
                if not person_visible_to(person, self.viewer):
                    continue
                person_count += 1
                hits.append(
                    TermHit(
                        term_id=person.person_id,
                        term=person.display_name,
                        definition=f"{person.display_name}（社内）",
                        scope=ScopeTags(
                            ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL
                        ),
                        kind=Kind.PERSON,
                    )
                )
        # The persons cap bounds ONE call; nothing bounds a session, and substring search
        # over display_name with ~50 common surname characters would reconstruct most of the
        # roster across many small calls. Leave a shape in the trail so that harvesting is at
        # least visible after the fact (best-effort, content-free — names never recorded).
        if person_count:
            self._trail("lookup_terms.persons", detail=f"n={person_count}")
        self._trail(RETRIEVAL_EVENT, detail="lookup_terms")  # R3b
        if not hits:
            self._mine_knowledge_candidates_best_effort(
                "search_failure", "lookup_terms:0", f"用語検索で該当なし: {query}"
            )
        return {"hits": [h.to_tool_output() for h in hits[:k]]}

    # --- TOOL log_usage (§4.2) — called every prompt (§6 rule 1) ---
    def log_usage(
        self,
        use_category: str,
        data_kinds: list[str],
        summary: str,
        client: str,
        request_id: str | None = None,
        policy_row_ids: list[str] | None = None,
        org_context: str | None = None,
    ) -> dict:
        """Record to the ledger, anchored to policy rows, and answer with advisory checks (§9).

        ``policy_row_ids`` are the rows (from ``get_applicable_policy``) the caller judged
        under — optional, so unaware clients keep zero friction (§9.3). The response carries
        ``suggested_category`` / ``mismatch`` / ``floor_warnings`` (with detours); the record is
        written regardless — advisory only, never a block (§9.4). The declared
        ``use_category`` goes to the ledger as-is (人の判断を尊重・責めない).

        SPEC_ops P0 guards: sessions declared external/private are refused outright (R1);
        ``use_category`` is normalized forgivingly (R8); a record with neither policy anchors
        nor an organization-related ``org_context`` is written as ``pending`` for steward review
        (R2 —
        with ``org_guard_strict=False`` an *absent* org_context still records).
        """
        from waca_loop.auth.identity import ledger_actor
        from waca_loop.guard import (
            LEDGER_WRITE_FAILED_MESSAGE,
            NEEDS_CONFIRMATION_MESSAGE,
            SCOPE_CONFIRMATION_MESSAGE,
            SCOPE_UNVERIFIED_MESSAGE,
            is_org_related,
            looks_non_org,
            normalize_data_kinds,
            normalize_request_id,
            normalize_use_category,
        )
        from waca_loop.policy.classify import classify_usage

        refusal = self._write_refusal()  # R1
        if refusal is not None:
            return refusal

        # R1b: a turn whose content carries another affiliation's signal must be *asked about*,
        # not written. Filing it as ``pending`` would already put the summary in the
        # organization's ledger, the exact thing 運用ルール2 forbids for 他組織/私事.
        #
        # This gate is NOT skipped while a scope is standing. `org` is exactly as sticky as
        # `external`/`private` — process-wide, per subject, until re-declared — so skipping on a
        # standing `org` would disable this gate for that person **forever**, across every later
        # conversation (and people are told to declare `org` to un-block themselves). A
        # declaration and this gate answer
        # different questions: the declaration says what the *person* is generally doing, this
        # says what *this turn's content* looks like. A standing `org` is not consent to file a
        # 確定申告 summary in the org's ledger, so it does not silence the question.
        # `looks_non_org` needs an explicit other-affiliation signal AND no organization signal, so
        # real organizational work stays quiet and the prompt does not become friction.
        if (
            not policy_row_ids
            # 宣言の状態は**見ない**。external/private は上の R1 で返り終えており、
            # 残るのは org と unknown — どちらもこの問いを免除する理由にならない。
            and looks_non_org(org_context, summary)
        ):
            self._trail("scope_confirmation_requested")
            return {
                "recorded": False,
                "needs_scope_confirmation": True,
                "message": SCOPE_CONFIRMATION_MESSAGE,
            }

        declared = UseCategory(normalize_use_category(use_category))  # R8
        # R9 — the vocabulary is the canonical enum plus whatever kinds the applicable policy
        # rows declare as triggers (§9.4), so normalization can never mute a rule.
        rules = self._applicable_policy_rules()
        kinds, unmapped_kinds = normalize_data_kinds(
            data_kinds, allowed_extra={k for r in rules for k in r.trigger_kinds}
        )

        # R2 org gate: policy anchors count as organization linkage; otherwise check org_context
        # (and summary as a fallback signal). Unrelated → pending, never silently recorded.
        status = "recorded"
        if not policy_row_ids:
            if org_context is not None:
                if not is_org_related(org_context, summary):
                    status = "pending"
            elif self.org_guard_strict and not is_org_related(summary):
                status = "pending"

        # R1d: if the scope store could not be read, "nothing declared" is a guess, and the
        # population it guesses about is unbounded (everyone who ever declared an exclusion and
        # never came back). Guessing "record" writes 業務外 into a ledger that R6 only cleans if
        # the person notices the row and withdraws it; guessing "refuse" loses the log.
        # `pending` is neither — the row exists, it is not counted as recorded, and
        # `review_pending_usage` lets a steward approve it or physically delete it.
        scope_unverified = False
        if status == "recorded" and self.scope_registry is not None:
            probe = getattr(self.scope_registry, "scope_unverified", None)
            if probe is not None:
                try:
                    _a_id, _a_raw = self._scope_actor()
                    scope_unverified = bool(probe(actor_id=_a_id, actor_raw=_a_raw))
                except Exception:  # noqa: BLE001 — a broken probe must not decide the write
                    scope_unverified = False
            if scope_unverified:
                status = "pending"

        actor_id, actor_raw = ledger_actor(self.viewer)
        written_at = datetime.now(UTC)
        record = UsageRecord(
            use_category=declared,
            data_kinds=kinds,  # R9 — canonical vocabulary only reaches the ledger
            summary=summary,  # summary only — no verbatim field exists (T6)
            client=client,
            actor_id=actor_id,
            actor_raw=actor_raw,
            # §4.2: id 形でなければ捨てる（自由文を台帳に入れない）
            request_id=normalize_request_id(request_id),
            ingested_via="mcp",
            policy_row_ids=list(policy_row_ids) if policy_row_ids else None,
            policy_version=max((r.version for r in rules), default=None) if rules else None,
            status=status,
        )
        # Is this the same write arriving twice? Asked HERE, not only inside the store,
        # because the answer changes what the *turn* means: a resend is not a newly recorded
        # turn, so it must not emit `usage_recorded` and must not close a policy_check in the
        # nudge. Leaving that to the store would make a reused `request_id` look like five
        # recorded turns backed by one summary. `dedupe=False` below then avoids asking the
        # same question twice.
        deduplicated = None
        if record.request_id:
            try:
                # One `now` for both halves: the probe's window and the row's occurred_at
                # must not straddle a clock tick.
                deduplicated = self.usage_store.existing_log_id(record, now=written_at)
            except Exception as exc:  # noqa: BLE001 — must not block the write
                # trail と同じ流儀: 黙って止まると「重複排除が効いていない」がどこにも残らない。
                # `waca-loop-ledger:` のマーカーでログ検索に引っかかる。
                logger.warning("waca-loop-ledger: dedup lookup failed: %s",
                               exc.__class__.__name__)
                deduplicated = None
        if deduplicated is not None:
            # detail は再利用した log_id（内容ではなく id）。これが無いと、抑止された1件を
            # 後から元の行に結びつけられない。
            self._trail("usage_resend_deduped", detail=deduplicated)  # NOT usage_recorded
            log_id = deduplicated
        else:
            try:
                log_id = self.usage_store.record(
                    record, occurred_at=written_at, dedupe=False
                )
            except Exception as exc:  # noqa: BLE001 — fixed message to the caller
                # Diagnosis stays here, as a class name; the caller gets a fixed line + code.
                logger.error("waca-loop-ledger: usage_log write failed: %s",
                             exc.__class__.__name__)
                # Best-effort, AFTER the failure — so a lost row has a row of its own.
                # Not `usage_recorded`: a failed write must not close a policy_check.
                self._trail("usage_write_failed")
                raise RuntimeError(LEDGER_WRITE_FAILED_MESSAGE) from None
            self._trail("usage_recorded", detail=status)  # R3a
        check = classify_usage(kinds, declared, rules)
        out = {"log_id": log_id, **check.to_tool_output()}
        if deduplicated is not None:
            # Say so. Otherwise a resend looks like a fresh write, and the caller cannot tell
            # that the row behind this id may be a `pending` one — or one its owner has since
            # withdrawn (R6 — the row is matched on purpose, see `existing_log_id`).
            out["deduplicated"] = True
        if unmapped_kinds:  # R9 — advisory, same 流儀 as mismatch: recorded anyway (§9.4)
            out["unmapped_data_kinds"] = unmapped_kinds
            out["data_kinds_recorded"] = kinds
        if status == "pending":
            out["status"] = "needs_confirmation"
            # Two different reasons reach `pending` and they need different words: R2 says
            # "this looks unrelated to the organization", R1d says "we could not check whether you
            # are excluded". Telling someone the first when it is the second sends them looking for
            # a problem in their own summary.
            out["message"] = (
                SCOPE_UNVERIFIED_MESSAGE if scope_unverified else NEEDS_CONFIRMATION_MESSAGE
            )
            if scope_unverified:
                out["scope_state"] = "unverified"
        return out

    # --- TOOL get_company_context (§4.2, optional wrapper) ---
    def get_company_context(self, query: str) -> dict:
        result = self.lookup_terms(query, k=5)
        used = result["hits"]
        if used:
            lines = [f"- {h['term']}: {h['definition']}" for h in used]
            context = "関連する組織の用語・人物:\n" + "\n".join(lines)
        else:
            context = "関連する組織用語は見つかりませんでした。"
        return {"context": context, "used_terms": used}

    # --- Lite Policy / Knowledge Store ---
    def get_personal_coach(
        self, task_hint_summary: str | None = None, max_items: int = 3
    ) -> dict:
        """Return personal AI-adoption recommendations (summary-only input)."""
        if self.policy_guide_store is None or self.adoption_map_store is None:
            raise RuntimeError("policy_guide_store/adoption_map_store not configured")
        from waca_loop.policy.coach import CoachRequest, PersonalCoach

        max_items = self._clamp_k(max_items, "personal_coach")
        coach = PersonalCoach(
            policy_guides=self.policy_guide_store,
            adoption_map=self.adoption_map_store,
        )
        response = coach.recommend(
            CoachRequest(
                viewer=self.viewer,
                task_hint_summary=task_hint_summary,
                mode="on_demand",
                max_items=max_items,
            )
        ).as_dict()
        response["recommendations"] = self._filter_personal_recommendations(
            response["recommendations"]
        )
        return response

    def export_personal_recommendations(self, max_items: int = 3) -> dict:
        """Return scheduled recommendation rows without writing to Sheets yet."""
        if self.policy_guide_store is None or self.adoption_map_store is None:
            raise RuntimeError("policy_guide_store/adoption_map_store not configured")
        from waca_loop.policy.coach import CoachRequest, PersonalCoach

        max_items = self._clamp_k(max_items, "personal_recommendations_export")
        coach = PersonalCoach(
            policy_guides=self.policy_guide_store,
            adoption_map=self.adoption_map_store,
        )
        response = coach.recommend(
            CoachRequest(viewer=self.viewer, mode="scheduled", max_items=max_items)
        )
        recommendations = self._filter_personal_recommendations(
            [r.as_dict() for r in response.recommendations]
        )
        return {
            "target_person_id": response.target_person_id,
            "not_for_evaluation": response.not_for_evaluation,
            "rows": [
                {
                    "recommendation_id": r["recommendation_id"],
                    "created_at": r["created_at"],
                    "target_person_id": response.target_person_id,
                    "not_for_evaluation": response.not_for_evaluation,
                    "title": r["title"],
                    "next_step": r["next_step"],
                    "safety_path": r["safety_path"],
                    "required_controls": r["required_controls"],
                    "source_policy_row_ids": r["source_policy_row_ids"],
                    "source_adoption_row_ids": r["source_adoption_row_ids"],
                    "status": r["status"],
                }
                for r in recommendations
            ],
        }

    def start_policy_workbench(
        self,
        goal_hint_summary: str | None = None,
        artifact_type: str | None = None,
        max_logs: int = 20,
    ) -> dict:
        """Start a log-aware policy coaching session."""
        from waca_loop.policy.workbench import PolicyWorkbench, WorkbenchRequest

        max_logs = self._clamp_k(max_logs, "policy_workbench")
        workbench = PolicyWorkbench(
            policy_guides=self.policy_guide_store,
            adoption_map=self.adoption_map_store,
            usage_reader=self.usage_store,
        )
        return workbench.start(
            WorkbenchRequest(
                viewer=self.viewer,
                goal_hint_summary=goal_hint_summary,
                artifact_type=artifact_type,
                max_logs=max_logs,
            )
        ).as_dict()

    def _filter_personal_recommendations(self, rows: list[dict]) -> list[dict]:
        """Gate 2 for personal recommendation output (§3.3)."""
        if self.viewer.person_id is None:
            return []
        person_keys = {self.viewer.person_id, f"person:{self.viewer.person_id}"}
        visible_units = set(self.viewer.units) | {ORG_UNIT}
        visible: list[dict] = []
        for row in rows:
            target = str(row.get("target") or "")
            if target and target not in visible_units and target not in person_keys:
                logger.warning("waca-loop: dropped cross-person coach recommendation")
                continue
            visible.append(row)
        return visible

    def mine_knowledge_candidates(
        self,
        source_kind: str,
        source_ref: str,
        summary: str,
        policy_row_ids: list[str] | None = None,
        owner_unit: str | None = None,
    ) -> dict:
        """Mine TERMS/SFAQ/policy candidates from a summary-only signal."""
        if self.candidate_store is None:
            raise RuntimeError("candidate_store not configured")
        from waca_loop.policy.candidate_miner import CandidateMiner, CandidateMiningRequest

        miner = CandidateMiner(candidate_store=self.candidate_store)
        return miner.mine(
            CandidateMiningRequest(
                viewer=self.viewer,
                source_kind=source_kind,
                source_ref=source_ref,
                summary=summary,
                policy_row_ids=list(policy_row_ids or []),
                owner_unit=owner_unit,
            )
        ).as_dict()

    def _mine_knowledge_candidates_best_effort(
        self,
        source_kind: str,
        source_ref: str,
        summary: str,
        policy_row_ids: list[str] | None = None,
        owner_unit: str | None = None,
    ) -> None:
        if self.candidate_store is None:
            return
        try:
            self.mine_knowledge_candidates(
                source_kind, source_ref, summary, policy_row_ids, owner_unit
            )
        except Exception:  # noqa: BLE001 — 候補化の失敗で読取/要望受付を止めない
            logger.warning("waca-loop: Lite knowledge candidate mining failed")

    # --- Survey / improvement loop ---
    def _respondent_token(self) -> str:
        if self.attribution == "anonymous":
            return "anon"
        return hash_subject(self.viewer.subject)

    # TOOL get_pending_survey — MCP-internal, skippable delivery.
    def get_pending_survey(self) -> dict:
        if self.survey_store is None:
            return {"none": True, "reason": "survey not configured"}
        campaign = current_campaign_id(self.cadence)
        if self.survey_store.has_responded(self._respondent_token(), campaign):
            return {"none": True, "campaign_id": campaign}
        return {"campaign_id": campaign, "questions": SURVEY_QUESTIONS}

    # TOOL submit_survey_response — collect (summary only) + 特定 + 課題化.
    def submit_survey_response(
        self,
        nps_score: int,
        nps_reason: str,
        useful: str,
        harmful: str,
        incident_flag: bool,
        incident_summary: str | None = None,
        contact_opt_in: bool = False,
        ref_log_id: str | None = None,
        ref_request_id: str | None = None,
        campaign_id: str | None = None,
        owner_unit: str | None = None,
        escalate: bool = False,
    ) -> dict:
        refusal = self._write_refusal()  # R1: 組織の業務外セッションは記録しない
        if refusal is not None:
            return refusal
        if self.survey_store is None:
            raise RuntimeError("survey_store not configured")
        campaign = campaign_id or current_campaign_id(self.cadence)
        respondent_id = self.viewer.person_id if self.attribution == "identified" else None
        response = SurveyResponse(
            nps_score=nps_score,
            nps_reason=nps_reason,
            useful_summary=useful,
            harmful_summary=harmful,
            incident_flag=incident_flag,
            campaign_id=campaign,
            respondent_token=self._respondent_token(),
            respondent_id=respondent_id,
            incident_summary=incident_summary,
            contact_opt_in=contact_opt_in,
            ref_log_id=ref_log_id,
            ref_request_id=ref_request_id,
            sensitivity=incident_sensitivity(incident_flag, escalate),
            # The owner is never picked from the viewer's units by set order: `org` is on
            # EVERY person's unit_path, and the sensitive gate is `owner_unit in
            # viewer.units` (§3.3), so an org-owned incident summary would be visible to the
            # whole organization, and the derived improvement task would inherit it. One
            # owning unit → that unit; ambiguous → the stewards (see _survey_owner_unit).
            # The resolved unit is echoed in the return value so the client can tell the
            # reporter who will see it.
            owner_unit=self._survey_owner_unit(owner_unit, escalate),
            escalated=escalate,
        )
        response_id = self.survey_store.record(response)

        tasks_out: list[dict] = []
        if self.task_store is not None:
            def _identify(text: str, log_id: str | None):
                return identify_content(
                    text, self.viewer, term_store=self.term_store,
                    embedder=self.embedder, explicit_log_id=log_id,
                )

            for task in make_tasks_from_response(response, response_id, identify=_identify):
                tid = self.task_store.add(task)
                tasks_out.append(
                    {
                        "task_id": tid,
                        "kind": task.kind.value,
                        "target_kind": task.target_kind,
                        "target_id": task.target_id,
                        "priority": task.priority,
                    }
                )
        identified = any(t["target_kind"] != "none" for t in tasks_out)
        return {
            "response_id": response_id,
            "tasks": tasks_out,
            "identified": identified,
            # Who this reaches. Surfaced deliberately: a reporter must be able to learn,
            # before trusting the channel, that an ambiguous unit routes to the stewards.
            "owner_unit": response.owner_unit,
        }

    # TOOL propose_candidates — record AI-extracted term/SFAQ/ERN candidates (AI提案).
    def propose_candidates(self, items: list[dict]) -> dict:
        """Record proposals. The proposed scope is carried verbatim onto the promoted row
        (improvement/candidates.py), so it is validated HERE, at the point a caller states it.

        Two things the caller does NOT get to decide freely:
        the tier defaults to ``dept``, not ``org`` — an AI-extracted proposal is not a
        decision to publish org-wide; and ``owner_unit`` must be one the proposer
        actually belongs to, so a proposal cannot be filed into another unit (§3.3).
        """
        refusal = self._write_refusal()  # R1: 組織の業務外セッションは記録しない
        if refusal is not None:
            return refusal
        self._require_identity("propose candidates")  # §5: 共有層への書き込みは帰属必須
        if self.candidate_store is None:
            raise RuntimeError("candidate_store not configured")
        from waca_loop.retrieval.acl import resolve_owner_unit

        # WACA Loop という道具への不満は、組織の判断知識ではない。抽出の**原料から外す**
        # (SPEC_improvement_requests §9.2)。混ぜると SFAQ が「使いにくい」の山になり、
        # 「なぜそう決めたか」を引くための層が用途を失う。
        #
        # 落とし方は **落とす（skip）** であって、バッチ全体の失敗ではない。途中で raise
        # すると、先に書き終えた候補だけが残って ids は返らず、呼び出し側は同じバッチを
        # 再送して重複を作る。判定は完全ではない（語の部分一致）ので、
        # 誤って落としたぶんが分かるよう、何をなぜ落としたかを戻り値で返す。
        skipped = [
            {
                "index": i,
                "reason": "WACA Loop 自身への要望。report_improvement_request で記録する (§9.2)",
            }
            for i, it in enumerate(items)
            if _is_about_the_tool(it.get("payload", {}))
        ]
        skipped_idx = {s["index"] for s in skipped}

        ids: list[str] = []
        for i, it in enumerate(items):
            if i in skipped_idx:
                continue
            scope_in = it.get("scope") or {}
            scope = ScopeTags(
                ScopeTier(scope_in.get("tier", "dept")),
                ScopeAudience(scope_in.get("audience", "internal")),
                ScopeSensitivity(scope_in.get("sensitivity", "normal")),
            )
            owner_unit = it.get("owner_unit")
            if scope.tier is ScopeTier.ORG:
                # Association-wide reach is a stewardship call, not a per-proposal option.
                if STEWARD_UNIT not in self.viewer.units:
                    raise PermissionError(
                        "only the steward may propose an org-tier candidate (§3.3)"
                    )
                # owner_unit still drives the sensitivity axis (_sensitivity_ok), so it is
                # validated on this branch too — just not required.
                if owner_unit is not None:
                    owner_unit = resolve_owner_unit(self.viewer, owner_unit)
            else:
                owner_unit = resolve_owner_unit(self.viewer, owner_unit)
            candidate = ExtractionCandidate(
                kind=CandidateKind(it["kind"]),
                payload=it.get("payload", {}),
                proposed_scope=scope,
                proposed_circles=it.get("circles", []),
                owner_unit=owner_unit,
                source=it.get("source", "prompt"),
                source_ref=it.get("source_ref"),
            )
            ids.append(self.candidate_store.add(candidate))
        out: dict = {"candidate_ids": ids}
        if skipped:
            out["skipped"] = skipped
        return out

    # TOOL approve_candidate — user approval; promotes a TERM into the grounding layer.
    def approve_candidate(
        self, candidate_id: str, approve: bool = True, edits: dict | None = None
    ) -> dict:
        refusal = self._write_refusal()  # R1: 組織の業務外セッションは記録しない
        if refusal is not None:
            return refusal
        self._require_identity("approve a candidate")  # §5: 共有層への書き込みは帰属必須
        if self.candidate_store is None:
            raise RuntimeError("candidate_store not configured")

        candidate = self.candidate_store.get(candidate_id)
        if candidate is None:
            # **読めない候補について決定を書かない。**
            #
            # ここで status だけ書いて返す形は3つ悪い:
            # (a) typo が誰の手でも「承認済み」として返る
            # (b) 種別が分からないので、下の種別ベースのゲートを**丸ごと迂回**する。
            #     本番の `BigQueryCandidateStore.get` は streaming buffer の行に対して
            #     正当に None を返しうるので、**実在する SFAQ 候補を非スチュワードが
            #     却下できる** — 「本番でだけゲートが開く」形になる
            # (c) 存在しない id と存在する id の区別を書き込みで作る
            #
            # 読めないなら断る。set_sfaq_status に入れたのと同じ原則。
            raise ValueError(f"candidate not found: {candidate_id}")
        # A servable SFAQ row is steward business either way (SPEC_phase2 §2.4 承認は人＝
        # スチュワード). The gate covers *rejection* too: because the decision is persisted,
        # an ungated reject would let any caller durably mark a steward-approved candidate
        # 'rejected' while its minted Question stays live and servable via search_sfaq.
        knowledge_sink = self.knowledge_store
        # 却下も steward 限定にする。決定が永続化される以上、ゲートの無い却下は
        # 「スチュワードが承認した候補を、誰でも durable に rejected にできる」を意味する
        # （しかも昇格済みの Question は search_sfaq で配信され続ける）。
        #
        # **判定は候補の KIND だけで行い、sink が配線されているかは見ない。**
        # `BigQueryTermStore` に `add` は無いので、能力ベースのゲートは**本番でだけ**
        # 「ゲート無し」に評価される — いちばん効いてほしい場所で開く。しかも後日 sink を
        # 配線した日に黙って塞がる（＝それまで開いていたことに誰も気づかない）。
        # deny-by-listing にし、ERN は sink が存在しない段階から列挙しておく（配線が
        # 漏れによって穴を開けないように）。
        # **承認も却下も、種別を問わず steward 限定。**
        #
        # 「却下は SFAQ だけ」にすると、「決定は終局」と組み合わさって、用語候補を
        # 非スチュワードが却下した時点でその候補は永久に承認できなくなる
        # （後からスチュワードが承認しようとすると `candidate already rejected` で落ちる）。
        # つまり非スチュワードが**共有語彙への提案に対する拒否権**を durable に持つ。
        # 二つの規則が単体では妥当でも、合わさると穴になる形である。
        #
        # 判定は候補の KIND だけで行い、sink が配線されているかは見ない（CLAUDE.md 不変条件。
        # 能力ベースだと `BigQueryTermStore` に `add` が無い**本番でだけ**ゲート無しになる）。
        if candidate.kind in (CandidateKind.SFAQ, CandidateKind.TERM) and (
            STEWARD_UNIT not in self.viewer.units
        ):
            verb = "approve" if approve else "reject"
            raise PermissionError(
                f"only the steward may {verb} a {candidate.kind.value} candidate "
                "(SPEC_phase2 §2.4)"
            )
        # **承認ゲートは冪等の早期 return より前に置く。** 順序が逆だと、
        # 「すでに承認済みの候補」を非スチュワードが再承認できてしまう
        # （新しい行は増えないが、durable な set_status を無権限の呼び手の名で書き、
        # 候補の存在と状態をその人に教えてしまう）。
        if approve and STEWARD_UNIT not in self.viewer.units:
            raise PermissionError(
                f"only the steward may approve a {candidate.kind.value} candidate "
                "(SPEC_phase2 §2.4)"
            )

        # Idempotency guard (SPEC_phase2 §2.4 / S8). The authoritative key is the reverse
        # link ``questions.attributes.candidate_id`` — NOT the candidate's current status,
        # which approve→reject→approve would reset, and NOT
        # ``payload.promoted_question_id``, which BigQueryCandidateStore never persists.
        # Checking BEFORE the mint also closes the mint-then-persist window: if the
        # set_status write below fails, the retry still finds the question and stops.
        minted = (
            knowledge_sink.find_by_candidate_id(candidate_id)
            if candidate.kind == CandidateKind.SFAQ
            and knowledge_sink is not None
            and hasattr(knowledge_sink, "find_by_candidate_id")
            else None
        ) or candidate.payload.get("promoted_question_id")
        # A decision is terminal. It is persisted (below), so contradicting an existing
        # decision must fail closed:
        #   approved → reject  would assert a retraction that never happens — the minted
        #     Question keeps being served by search_sfaq (there is no retract path here;
        #     set_sfaq_status is the deliberate, separate step).
        #   rejected → approve would let a later caller override a human's 却下 ("承認は人").
        if candidate.status in (CandidateStatus.APPROVED, CandidateStatus.REJECTED):
            decided = candidate.status is CandidateStatus.APPROVED
            if decided != approve:
                raise ValueError(
                    f"candidate already {candidate.status.value} — a decision is terminal. "
                    "To retract a promoted SFAQ row use set_sfaq_status on the question "
                    "itself (SPEC_phase2 §2.4)."
                )
        if approve and (minted or candidate.status == CandidateStatus.APPROVED):
            if edits:
                raise ValueError(
                    "candidate already approved — re-approving cannot apply edits; "
                    "amend the promoted question instead (SPEC_phase2 §2.4)"
                )
            self.candidate_store.set_status(candidate_id, CandidateStatus.APPROVED)
            result = {
                "candidate_id": candidate_id,
                "status": CandidateStatus.APPROVED.value,
                "promoted": False,
                "already_approved": True,
            }
            # The reverse-link lookup is viewer-independent by design, and the id it yields
            # is a write handle (set_sfaq_status is visibility-blind). Only echo it back if
            # the caller could see the row anyway — otherwise say it exists and stop.
            if minted:
                if knowledge_sink is not None and knowledge_sink.get(minted, self.viewer):
                    result["question_id"] = minted
                else:
                    result["question_id_withheld"] = True  # exists, but not yours to see
            return result
        term_sink = self.term_store if hasattr(self.term_store, "add") else None
        status = _approve_candidate(
            candidate,
            approve,
            term_sink=term_sink,
            knowledge_sink=knowledge_sink,
            approver_id=self.viewer.person_id,
            edits=edits,
        )
        # Persist the decision. ``_approve_candidate`` mutates the ExtractionCandidate it was
        # handed; with the in-memory store that object *is* the stored row, but the BigQuery
        # store returns a detached copy — without this write the candidate stays 'proposed'
        # forever and the proposal box shows already-promoted rows as unapproved.
        self.candidate_store.set_status(candidate_id, status)
        promoted = bool(
            approve
            and (
                (candidate.kind == CandidateKind.TERM and term_sink is not None)
                or (
                    candidate.kind == CandidateKind.SFAQ
                    and knowledge_sink is not None
                )
            )
        )
        result = {"candidate_id": candidate_id, "status": status.value, "promoted": promoted}
        if approve and (question_id := candidate.payload.get("promoted_question_id")):
            # 冪等側の出口（上）と**同じ規則**にする。id は書き込みハンドルになりうるので、
            # 見える人にだけ返す。steward 限定の経路なので実害は無いが、同じ関数の
            # 2つの出口が別の規則で動いていると、いつか片方だけ緩む。
            if knowledge_sink is not None and knowledge_sink.get(question_id, self.viewer):
                result["question_id"] = question_id
            else:
                result["question_id_withheld"] = True
        return result

    # TOOL list_improvement_tasks — admin view of derived tasks, ACL-filtered by viewer (§3.3).
    def list_improvement_tasks(
        self, status: str | None = None, kind: str | None = None
    ) -> dict:
        if self.task_store is None:
            return {"tasks": []}
        # Gate 1: store filters by viewer (SQL WHERE in production); Gate 2: in-process re-check.
        from waca_loop.improvement.acl import task_visible_to

        rows = self.task_store.list(status=status, kind=kind, viewer=self.viewer)
        rows = [r for r in rows if task_visible_to(r, self.viewer)]
        # FLOOR-02: the task list is a corpus too, and an incident-derived one — the most
        # sensitive rows in the system. The store returns everything visible with no LIMIT.
        return {"tasks": rows[: self._clamp_k(len(rows), "improvement_tasks")]}

    # --- 改善要望 (docs/SPEC_improvement_requests.md) ---------------------------------
    #
    # `improvement_tasks` と別立てなのは §2 のとおり。あちらは申告由来で「組織の仕事」の記録、
    # こちらは「WACA Loop という道具への注文」。寿命が違うものを同じ層に置かない。

    def _mirror_requests_to_sheet(self) -> str | None:
        """要望をシートへ即時反映する（**best-effort**・§10）。

        WACA Loop が正でシートは鏡なのだから、**鏡が曇っても本体は止めない**。ここで例外を
        伝播させると、Sheets API の都合で組織の記録そのものが落ちる — 正と鏡の関係が
        逆転する。取りこぼしは夜間の `export_requests_to_sheet.py` が埋める。
        """
        if self.request_sheet_mirror is None:
            return None
        try:
            self.request_sheet_mirror()
        except Exception:  # noqa: BLE001 — 鏡の失敗で本体を止めない (§10)
            # ログだけでは誰も読まない（Cloud Run の warning は誰のダッシュボードでもない）。
            # 呼び出し側に返して、スキルが一行で口に出せるようにする。
            logger.warning("waca-loop: シートへの即時反映に失敗（夜間バッチで埋めます）")
            return "failed"
        return None

    # TOOL report_improvement_request — WACA Loop を使っている最中に気づいたことを残す (§3.1)。
    def report_improvement_request(
        self,
        kind: str,
        title: str,
        body: str,
        anonymous: bool = False,
        owner_unit: str | None = None,
    ) -> dict:
        """改善要望を記録する。

        **会話からの自動抽出はしない**（§9.2）。この明示的な呼び出しでしか入らないから、
        SFAQ の候補抽出と混ざらない。道具への不満は組織の判断知識ではない。
        """
        refusal = self._write_refusal()  # R1: 組織の業務外セッションは記録しない
        if refusal is not None:
            return refusal
        if self.request_store is None:
            raise RuntimeError("request_store not configured")
        from waca_loop.models import ImprovementRequest, RequestKind, RequestStatus
        from waca_loop.retrieval.acl import resolve_owner_unit

        self._require_identity("report an improvement request")  # §5 共有層への書き込み
        title = (title or "").strip()
        body = (body or "").strip()
        if not title or not body:
            raise ValueError("title と body は必須です")

        from waca_loop.models import (
            ScopeAudience,
            ScopeSensitivity,
            ScopeTags,
            ScopeTier,
        )

        # 匿名は **person_id を落とすだけでは成立しない**。所属ユニットを刻むと、数名しか
        # いない単位から出た匿名要望は owner_unit だけで起案者が割れる。だから匿名のときは
        # 所有単位を持たせず、org 階層で受ける — 匿名を選んだ人に「どの部から出たか」
        # を差し出させない。
        # 代償は、その要望が全社可視になること。匿名にするとはそういうことである、と
        # ツール説明で先に伝える。
        if anonymous:
            owner = None
            scope = ScopeTags(
                ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL
            )
        else:
            # 所有単位は推測しない。複数所属なら呼び出し側が名指しする。
            owner = resolve_owner_unit(self.viewer, owner_unit)
            scope = ScopeTags(
                ScopeTier.DEPT, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL
            )

        request = ImprovementRequest(
            kind=RequestKind(kind),
            title=title,
            body=body,
            channel="mcp",
            reporter_id=None if anonymous else self.viewer.person_id,
            reporter_label="匿名" if anonymous else None,
            owner_unit=owner,
            scope=scope,
            # WACA Loop 経由は OAuth で本人性が担保済みなので、フォーム経由と違って unverified に
            # 落とさない (§3.2 との対比)。
            status=RequestStatus.OPEN,
        )
        request_id = self.request_store.add(request)
        # 匿名のときは計器盤にも残さない。`_trail` は actor_id 付きで時刻を刻むので、
        # 匿名行と同時刻のイベントを突き合わせれば起案者が秒精度で一意に決まる。
        # 件数を数えたい気持ちより、匿名の約束が先。
        if not anonymous:
            self._trail("improvement_request_reported", detail=kind)
            self._mine_knowledge_candidates_best_effort(
                "improvement_request",
                f"improvement_request:{request_id}",
                f"{kind}: {title}",
                owner_unit=owner,
            )
        out = {"request_id": request_id, "status": RequestStatus.OPEN.value}
        mirror = self._mirror_requests_to_sheet()
        if mirror:
            out["sheet_mirror"] = mirror
        return out

    # TOOL list_improvement_requests — 未対応の要望。スキル improvement-requests が §4 で読む。
    def list_improvement_requests(
        self, status: str | None = None, kind: str | None = None
    ) -> dict:
        """要望の一覧（ACLで絞り込み済み）。

        既定で ``open`` だけを返す。ログイン不要フォーム由来の ``unverified`` を
        開発者の作業キューに直結させないための既定である（§3.2）— 明示的に
        ``status="unverified"`` と指定した仕分け役だけがそれを見る。
        """
        if self.request_store is None:
            return {"requests": []}
        from waca_loop.improvement.requests import request_visible_to
        from waca_loop.models import RequestStatus

        wanted = status if status is not None else RequestStatus.OPEN.value
        # Gate 1: ストアが絞る（本番は SQL WHERE）／Gate 2: in-process で再確認 (§3.3)
        rows = self.request_store.list(status=wanted, kind=kind, viewer=self.viewer)
        rows = [r for r in rows if request_visible_to(r, self.viewer)]
        return {"requests": rows[: self._clamp_k(len(rows), "improvement_requests")]}

    # TOOL set_request_status — 状態を進める。wontfix だけ権限が違う (§8)。
    def set_request_status(
        self, request_id: str, status: str, resolution: str | None = None
    ) -> dict:
        """要望の状態を更新する。

        ``wontfix``（見送り）だけは **`owner` ユニット（org.toml [units] owner）限定**。ほかの状態は
        どれも途中経過の記録で、間違えれば次の更新で直る。見送りは「直さないと決めた」
        結論で、起案者への説明責任が伴い、放っておくとその要望は二度と §4 の提示に
        出てこない。記録の権限と、打ち切る権限は別物である（§8）。
        """
        refusal = self._write_refusal()  # R1
        if refusal is not None:
            return refusal
        if self.request_store is None:
            raise RuntimeError("request_store not configured")
        from waca_loop.improvement.requests import request_visible_to
        from waca_loop.models import RequestStatus
        from waca_loop.retrieval.acl import OWNER_UNIT

        self._require_identity("change a request status")
        new_status = RequestStatus(status)
        if new_status is RequestStatus.WONTFIX and OWNER_UNIT not in self.viewer.units:
            raise PermissionError(
                "見送り(wontfix)を決められるのは WACA Loop オーナーだけです (§8)"
            )
        # 見えない要望を手探りで書き換えられないこと（blind write の禁止）。
        row = self.request_store.get(request_id, self.viewer)
        if row is None or not request_visible_to(row, self.viewer):
            raise ValueError(f"request not found or not visible: {request_id}")
        self.request_store.set_status(
            request_id, new_status, viewer=self.viewer, resolution=resolution
        )
        self._trail("request_status_set", detail=f"{request_id}->{new_status.value}")
        out = {"request_id": request_id, "status": new_status.value}
        mirror = self._mirror_requests_to_sheet()
        if mirror:
            out["sheet_mirror"] = mirror
        return out

    def _applicable_policy_rules(self) -> list:
        """The viewer's policy rows through both ACL gates (shared by serve + classify paths).

        Gate 1 is the store (SQL WHERE in production); Gate 2 re-checks in-process on the
        full row so it always sees every field Gate 1 does (no under-enforcement if a
        future axis is added to the shared rule).
        """
        if self.policy_store is None:
            return []
        from waca_loop.policy.acl import policy_visible_to  # Gate 2: in-process re-check
        from waca_loop.policy.store import _policy_to_row

        rules = self.policy_store.applicable(self.viewer)  # Gate 1
        return [r for r in rules if policy_visible_to(_policy_to_row(r), self.viewer)]

    # TOOL get_applicable_policy — 配達係 (SPEC_phase1 §9): serve the viewer's rows.
    def get_applicable_policy(self, task_hint: str | None = None) -> dict:
        """Serve the viewer's applicable policy (org floor + their dept rows) grouped by 採用レベル.

        Business rows group under ``level2`` (そのまま採用) / ``level1`` (条件付き) /
        ``level0`` (未使用); the ``floor`` list carries the absolute bright-lines (with their
        迂回路). ``legend`` explains the vocabulary so the consuming model needs no side channel.
        ``task_hint`` is accepted but does not rank; all applicable rows are returned.
        WACA Loop only *reads* here — it never approves or edits the human-authored rows
        (SPEC_phase1 §9.6 境界).
        """
        self._trail("policy_check")  # R3a: observable half of the per-turn contract
        # Computed AFTER the trail so the check being served right now is the one the hint
        # discounts as in-flight; before it, the *previous* turn would be discounted instead
        # and a real miss would go unmentioned. (``_trail`` is best-effort: if THIS event is
        # the one that fails to land, the previous turn is discounted after all and a real
        # miss goes unmentioned. Silence-biased, so it stays a miss we can live with.)
        hint = self._coverage_hint()  # C4: advisory, self-scoped, may be None
        # R1c: the standing scope announces itself here, not only when a write is
        # refused. The declaration outlives the conversation that made it, so a new
        # conversation otherwise discovers it by losing its first log_usage — and every turn
        # before someone reads that refusal is silently unrecorded. This is the earliest
        # honest moment to say it: the 運用ルール already sends the model here at the top of
        # the turn. Complement of C4, which deliberately does NOT nudge an excluded session —
        # that session is not failing to record, it is excluded on purpose.
        scope_notice = self._standing_scope_notice()
        expiry_warning = self._scope_expiry_warning()   # 失効直前だけ。ふだんは None
        lapse_notice = self._scope_lapse_notice()      # 失効した瞬間に1回だけ
        vanished_notice = self._scope_vanished_notice()   # 保持期間で行が消えたとき1回だけ
        empty: dict = {
            # `legend` stays an empty dict whenever no rule is visible: a viewer the ACL
            # gives zero rows (e.g. a partner) shouldn't receive org vocabulary either.
            "level2": [], "level1": [], "level0": [], "floor": [], "legend": {},
            "version": None,
        }
        def _with_scope(out: dict) -> dict:
            """Attach the scope fields — and only those — to a response.

            The coverage hint stays off `empty`: it is org-internal vocabulary (it names
            `log_usage` and asserts a per-person coverage ledger exists), and a viewer we will
            not tell the rules to is not one we nag about them.

            The **scope** notices are a different kind of fact — they are about the caller's own
            writes being refused right now, not about the org's rules — and `empty` is reached by
            ordinary members too: a unit with no rows, or rows not yet `approved`. Withholding
            them there would mean the one-shot lapse notice is *consumed* above and dropped on
            the floor, leaving a member with a permanent, unexplained block: the exact
            「拒否されているのに理由が出ない」 shape R1d exists to close.

            **Org members only.** A non-member reaching `empty` is the partner case, and they
            still get the reason at the refusal itself (`_write_refusal` carries it); what they do
            not get is a pre-emptive line
            confirming that WACA Loop keeps a per-person scope registry.
            """
            if not getattr(self.viewer, "is_org_member", False):
                return out
            if scope_notice:
                out["scope_notice"] = scope_notice
            if expiry_warning:
                out["scope_expiry_warning"] = expiry_warning
            if lapse_notice:
                out["scope_lapsed"] = lapse_notice
            if vanished_notice:
                out["scope_vanished"] = vanished_notice
            return out

        if self.policy_store is None:
            return _with_scope(empty)
        rules = self._applicable_policy_rules()
        if not rules:
            return _with_scope(empty)

        def _row(r) -> dict:
            return {
                "row_id": r.row_id, "line": r.line, "effect": r.effect, "level": r.level,
                "scope_unit": r.scope_unit, "is_floor": r.is_floor, "status": r.status,
            }

        def _bucket(r) -> int:
            # An ACL-visible row must never silently vanish from the response: a business
            # row with a NULL level (hand-edited in BQ past the seeder's validation) lands
            # in level1 — the same conservative default classify.py uses (§9.4).
            return r.level if r.level in (2, 1, 0) else 1

        by: dict[int, list[dict]] = {2: [], 1: [], 0: []}
        for r in rules:
            if not r.is_floor:
                by[_bucket(r)].append(_row(r))
        out = {
            "level2": by[2],
            "level1": by[1],
            "level0": by[0],
            "floor": [_row(r) for r in rules if r.is_floor],
            "legend": {
                "level2": "採用レベル2 — そのまま採用してよい行為",
                "level1": "採用レベル1 — 条件付き（effect の条件・安全チャネル経由で）",
                "level0": "採用レベル0 — 未使用（AI利用を差し控える行為）",
                "floor": "禁止床(is_floor) — 絶対の下限。effect の迂回路を案内する",
            },
            "version": max((r.version for r in rules), default=None),
        }
        if hint:
            out["coverage_hint"] = hint   # C4: absent when there is nothing to say
        return _with_scope(out)

    # --- SFAQ — 判断ナレッジ層 (SPEC_phase2 §3) ---
    # --- TOOL find_expert (ERN・SPEC_phase3 §3) ---
    def find_expert(self, topic: str, k: int = 3) -> dict:
        """その領域に詳しい人を最大3名（E2）。**順位・スコア・件数は出さない。**

        発火は**質問者起点のみ**。誰かを常時ランキングする経路を作らない（§0）。

        ゲートは3つ、すべて既存の実装に委ねる（E11: ここに可視性のロジックを書かない）:

        - **Gate1（§4b）** — 集計対象の問いは質問者の可視範囲を継承する。ERN 独自の
          可視範囲を持たない。`knowledge.acl` の二重ゲートをそのまま使う。
        - **§4a** — 著者マスク該当行は `ern.engine.routable_feedback` が集計前に落とす。
        - **Gate2（§4c）** — 名前を出してよい人は `person_visible_to` が決める。
          **キャッシュを置かない** — opt-out が次の呼び出しで即時に効く（E5）。

        上限は `_clamp_k(k, "find_expert")`。**新しい上限機構を作らない**（§0 前提）。
        `find_expert` は `persons` と同じく break-glass の対象外
        （`docs/ACCESS_MATRIX.md` §1）。
        """
        from waca_loop.ern.engine import build_candidates
        from waca_loop.ern.schema import assert_no_scores

        limit = self._clamp_k(k, "find_expert")
        empty = assert_no_scores({"candidates": []})

        # 非 org メンバーは常に空（fail closed・§4c）。ストア未配線も同じ。
        #
        # 拒否も計器盤に残す。開示はゼロなので漏れではないが、**いちばん見たいのは
        # 「人検索を試みた非メンバー」**で、そこだけ痕跡が残らないのは観測の穴になる。
        # topic は入れない。
        if not self.viewer.is_org_member:
            self._trail("find_expert", detail="refused=not_org_member")
            return empty
        if self.knowledge_store is None or self.person_store is None:
            self._trail("find_expert", detail="refused=unwired")
            return empty

        from waca_loop.knowledge.acl import question_visible_to
        from waca_loop.knowledge.store import question_to_row

        # Gate1: 質問者に見える問いだけ。store が Gate1（本番は SQL WHERE）、ここで Gate2。
        #
        # リコールの k は候補数の上限（3）ではなく `search_sfaq` の上限に乗せる。3件しか
        # 引かないと、その領域で最も語られている問い3つに紐づく人しか候補になり得ず、
        # 「誰に聞けばよいか」ではなく「どの問いが目立つか」を返す道具になる。
        pool = self.knowledge_store.search(
            topic, self.viewer, self._clamp_k(None, "search_sfaq")
        )
        visible = [
            q for q in pool if question_visible_to(question_to_row(q), self.viewer)
        ]
        by_qid = self.knowledge_store.feedback_for_many(
            [q.question_id for q in visible], self.viewer
        )

        people = self._ern_people(visible, by_qid, topic)
        declared = (
            self.ern_assertion_store.topics_by_person(self.viewer)
            if self.ern_assertion_store is not None
            else {}
        )

        candidates = build_candidates(
            viewer=self.viewer,
            topic=topic,
            questions=visible,
            feedback_by_question=by_qid,
            people=people,
            declared=declared,
            half_life_days=self.ern_half_life_days,
            now=datetime.now(UTC),
            role_fallback=list(self.ern_role_fallback.get(topic, ())),
            limit=limit,
        )

        # ③ 信頼度 = append-only の証跡そのもの（§2.3）。**候補者 id はここにだけ残る。**
        self._ern_audit(topic, candidates)
        # E12(b) 計器盤: 台帳には載せるが、**候補者名・person_id を入れない**（E13）。
        self._trail("find_expert", detail=f"topic_len={len(topic or '')}")

        return assert_no_scores(
            {"candidates": [c.as_dict() for c in candidates]}
        )

    def _ern_people(self, questions: list, by_qid: dict, topic: str) -> dict:
        """候補になりうる person を、**ゲートを通して**引く（§3.3 の SQL 側）.

        engine 側の `disclosable`（`person_visible_to`）が in-process 側の再確認になる。
        二重ゲートは「別々の2つの判断」ではなく「**同じ判断を2箇所で**」なので、ここで
        ゲートを掛けても判断が散るわけではない — 規則の出どころは `retrieval.acl` の
        1箇所のままである。
        """
        # **宣言は候補を生まない。** `build_candidates` が選ぶのはシグナル由来か役職
        # フォールバックだけで、宣言だけの人は候補にならない（宣言は「すでに選ばれた人に
        # `declared_topics` を添える」役）。ここで id を混ぜているのはそのための引き当てで、
        # 候補源を増やしているのではない — 将来ここを候補源にするなら、§4a で落とした
        # 著者が宣言経由で名前として出ないことを別途確かめること。
        ids: set[str] = set()
        for fbs in by_qid.values():
            ids |= {f.author_id for f in fbs if f.author_id}
        if self.ern_assertion_store is not None:
            ids |= set(self.ern_assertion_store.persons_for_topic(topic, self.viewer))
        ids |= set(self.ern_role_fallback.get(topic, ()))
        if not ids:
            return {}
        # **ゲート済みの一括取得**を使う。`get` は1行を id で引くだけでゲートを持たないので、
        # ここで流用すると、オプトアウトした同僚の行が丸ごと BigQuery から出てきて、
        # 絞り込みが in-process だけになる（§3.3 の片側が欠ける）。
        return self.person_store.get_many(sorted(ids), self.viewer)

    def _ern_audit(self, topic: str, candidates: list) -> None:
        """§2.3 ③。証跡が残せなくても検索は落とさないが、**握り潰しも黙ってはしない**.

        `topic` は `audit_topic` を通してから残す — 呼び出し側の文字列がそのまま表に
        書かれるのを止める（INV-1/E8。DDL の「問い合わせ本文ではない」を実際に守らせる）。
        """
        from waca_loop.ern.engine import audit_topic

        if self.ern_audit_store is None:
            return
        try:
            from waca_loop.auth.identity import ledger_actor

            actor_id, actor_raw = ledger_actor(self.viewer)
            self.ern_audit_store.record(
                actor_id=actor_id, actor_raw=actor_raw, topic=audit_topic(topic),
                candidate_ids=[c.person_id for c in candidates],
                evidence_types=[c.evidence_type for c in candidates],
            )
        except Exception:  # noqa: BLE001 — 証跡の失敗で人検索そのものは止めない
            logger.warning("ern_audit write failed; the search itself was not blocked")

    def search_sfaq(self, query: str, k: int = 5, reason: str | None = None) -> dict:
        """Past questions + their accrued judgments for ``query`` (read-only).

        Gate 1 lives in the store (SQL WHERE in production); Gate 2 re-checks here.
        E-3(ii) rows are served with feedback authors masked (本人特定を伏せて共有).
        Status is always surfaced — 暫定/係争中 knowledge ships with its label, never
        silently as truth (SPEC_phase2 §2.4).
        """
        if self.knowledge_store is None:
            return {"hits": []}
        from waca_loop.knowledge.acl import mask_author, question_visible_to
        from waca_loop.knowledge.store import question_to_row

        questions = self.knowledge_store.search(query, self.viewer, self._clamp_k(
            k, "search_sfaq", reason
        ))  # FLOOR-02
        hits = []
        # Gate 2 を先に通してから、残った問いのフィードバックを **1回で** 引く。
        # 1件ずつ引くと k 件ぶんのクエリになり、feedback は question_id で
        # クラスタリングされていないのでそのたびに走査が走る。
        visible = [q for q in questions if question_visible_to(question_to_row(q), self.viewer)]
        by_qid = self.knowledge_store.feedback_for_many(
            [q.question_id for q in visible], self.viewer
        )
        for q in visible:
            row = question_to_row(q)
            masked = mask_author(row)
            # FLOOR-02: k bounds the QUESTIONS, but each one fans out to every feedback row
            # ever accrued (verbatim bodies, no LIMIT in the SQL). A hot question with
            # hundreds of judgments would ship an unbounded volume under a k of 20.
            fbs = by_qid.get(q.question_id, [])
            fbs = fbs[: self._clamp_k(len(fbs), "sfaq_feedback")]
            hits.append({
                "question_id": q.question_id,
                "question_text": q.question_text,
                "status": q.status.value,
                "provenance": q.provenance.value,
                "feedback": [
                    {
                        "fb_type": f.fb_type.value,
                        "body": f.body,
                        "author_id": None if masked else f.author_id,
                    }
                    for f in fbs
                ],
            })
        self._trail(RETRIEVAL_EVENT, detail="search_sfaq")  # R3b
        if not hits:
            self._mine_knowledge_candidates_best_effort(
                "search_failure", "search_sfaq:0", f"SFAQ検索で該当なし: {query}"
            )
        return {"hits": hits}

    def _sibling_evidences(self, task_id: str) -> list[str]:
        """同じ申告から派生したタスク**全部**の evidence（見えるものだけ）。

        ひとつの回答から incident / harmful / nps_detractor の3つが立つので、A を指しながら
        B の本文を貼る抜け道がある。表口も裏口も、元タスク1本ではなくこれを見る。
        """
        if not task_id or self.task_store is None:
            return []
        from waca_loop.improvement.acl import task_visible_to

        rows = self.task_store.list(status=None, kind=None, viewer=self.viewer)
        task = next((r for r in rows if r.get("task_id") == task_id), None)
        if task is None or not task_visible_to(task, self.viewer):
            return []
        response_id = task.get("source_response_id")
        if not response_id:
            return [task.get("evidence") or ""]
        # 見えないタスクの本文は corpus から落ちる。可視範囲を広げるほうが害が大きいので、
        # その文を別経路で手に入れて貼られる残余は受け入れる。
        return [
            r.get("evidence") or ""
            for r in rows
            if r.get("source_response_id") == response_id and task_visible_to(r, self.viewer)
        ]

    def _refuse_report_copy(self, evidences: list[str], texts: dict[str, str | None]) -> None:
        """申告の本文・個人情報・在籍者の氏名が、共有される行に残っていないか (SPEC_phase2 §2.4)。

        **表口（`raise_lesson_from_task`）と裏口（`add_sfaq_feedback`）の両方がここを通る。**
        裏口に弱い実装（`evidence_leak`）を使うと、句読点を挟んだ部分貼り／**兄弟タスク**の
        evidence／氏名・連絡先の3つが素通りする。

        ガードを2つ持つと、弱いほうが本番の経路に残っていても気づけない。**1つにする。**
        """
        from waca_loop.improvement.lesson_guard import (
            borrows_evidence,
            names_in,
            pii_kinds,
        )

        for label, body in texts.items():
            if not body:
                continue
            if evidences and borrows_evidence(body, evidences):
                raise ValueError(
                    f"{label}に申告の本文が写されている（部分的な引き写しを含む）。"
                    "共有してよい形（誰の話かが消えた一般化）に書き直してください"
                )
            kinds = pii_kinds(body)
            if kinds:
                raise ValueError(
                    f"{label}に個人を特定できる情報が残っている（{'/'.join(kinds)}）。"
                    "この行は全社に置かれます。固有名詞・連絡先を外して書き直してください"
                )
            if self.person_store is None:
                raise RuntimeError(
                    "person_store が無いため氏名の検査ができない。書き込みを中止した"
                )
            if names_in(body, self.person_store.roster_names()):
                raise ValueError(
                    f"{label}に在籍者の氏名が残っている。"
                    "この行は全社に置かれます。誰の話かが消えた形に書き直してください"
                )

    def _refuse_evidence_copy(self, question: Any, texts: dict[str, str | None]) -> None:
        """学びの行に、元の申告本文が写し込まれていないか — **表口と同じ検査**を通す。

        対象は **`raise_lesson_from_task` が起こした行だけ**（`attributes.source_task_id`
        を持つ行）。そこが「きれいな学びを起こす → 生の本文を足す」で迂回された場所である。
        通常の問いへのフィードバックまで広げるのは別の判断なので、ここでは広げない
        （広げるなら独立した判断として行う）。
        """
        attrs = getattr(question, "attributes", None) or {}
        task_id = str(attrs.get("source_task_id") or "")
        if not task_id:
            return
        self._refuse_report_copy(self._sibling_evidences(task_id), texts)

    def add_sfaq_feedback(self, question_id: str, fb_type: str, body: str) -> dict:
        """Record feedback on a question (write; フィードバックB 復唱→確認→感謝の記録先).

        ``fb_type`` is one of the six S5 types, assigned by the calling AI — humans
        never label. The question must be visible to the viewer (no blind writes
        against rows the caller cannot see).
        """
        refusal = self._write_refusal()  # R1: 組織の業務外セッションは記録しない
        if refusal is not None:
            return refusal
        self._require_identity("record SFAQ feedback")  # §5: 共有層への書き込みは帰属必須
        if self.knowledge_store is None:
            raise RuntimeError("knowledge_store not configured (SFAQ)")
        from waca_loop.knowledge.store import new_id
        from waca_loop.models import FbType, Feedback

        question = self.knowledge_store.get(question_id, self.viewer)
        if question is None:
            raise ValueError(f"question not found or not visible: {question_id}")
        # 学びの行に、あとから申告本文を積み直す道を塞ぐ。raise_lesson_from_task 側だけを
        # 見ていると、「きれいな学びを起こす → この1コールで生の本文を足す」の2手で
        # 全社公開に到達できてしまう。
        self._refuse_evidence_copy(question, {"判断の本文": body})
        fb = Feedback(
            feedback_id=new_id("fb"),
            question_id=question_id,
            fb_type=FbType(fb_type),
            body=body,
            author_id=self.viewer.person_id,
        )
        self.knowledge_store.add_feedback(fb)
        return {"feedback_id": fb.feedback_id, "recorded": True}

    def add_sfaq_question(
        self,
        question_text: str,
        first_feedback: str | None = None,
        owner_unit: str | None = None,
    ) -> dict:
        """File a new question as a *candidate* (write; 生ログ→候補は自動, §2.4).

        The contributor is deliberately sharing via the tool, so provenance=individual
        with perm_individual=true (本人合意 = the act of filing).

        Scope is **dept/internal/normal owned by the filer's unit** (§3.3), NOT org-wide: a
        filed question is one person's unreviewed account of a situation, and the org tier
        would make every such row visible to the whole organization the moment it lands.
        ``owner_unit`` is required only when the filer belongs to more than one unit — see
        :func:`resolve_owner_unit`, which refuses rather than guessing.

        Widening a dept-scoped question to org is a separate steward-only step
        (:meth:`set_sfaq_scope`); ``set_sfaq_status`` moves ``status`` only.
        """
        refusal = self._write_refusal()  # R1: 組織の業務外セッションは記録しない
        if refusal is not None:
            return refusal
        self._require_identity("file an SFAQ question")  # §5: 共有層への書き込みは帰属必須
        if self.knowledge_store is None:
            raise RuntimeError("knowledge_store not configured (SFAQ)")
        from waca_loop.knowledge.store import new_id
        from waca_loop.models import (
            FbType,
            Feedback,
            Provenance,
            Question,
            QuestionStatus,
        )
        from waca_loop.retrieval.acl import resolve_owner_unit

        q = Question(
            question_id=new_id("q"),
            question_text=question_text,
            owner_unit=resolve_owner_unit(self.viewer, owner_unit),
            scope=ScopeTags(ScopeTier.DEPT, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL),
            provenance=Provenance.INDIVIDUAL,
            perm_individual=True,
            status=QuestionStatus.CANDIDATE,
            source="mcp",
        )
        self.knowledge_store.add_question(q)
        if first_feedback:
            self.knowledge_store.add_feedback(Feedback(
                feedback_id=new_id("fb"),
                question_id=q.question_id,
                fb_type=FbType.OPEN,
                body=first_feedback,
                author_id=self.viewer.person_id,
            ))
        return {"question_id": q.question_id, "status": q.status.value}

    def set_sfaq_status(self, question_id: str, status: str) -> dict:
        """Steward-only state transition (approve/contest — 承認は人, §2.4).

        Anyone may raise a dispute in conversation, but the state machine itself is
        moved only by the steward (steward unit).
        """
        if self.knowledge_store is None:
            raise RuntimeError("knowledge_store not configured (SFAQ)")
        from waca_loop.models import QuestionStatus

        # The steward check below already refuses an unresolved viewer (empty units), but
        # that keys on membership, not attribution — state the attribution rule directly.
        self._require_identity("change question status")
        if STEWARD_UNIT not in self.viewer.units:
            raise PermissionError("only the steward may change question status (§2.4)")
        # **読めない行は動かせない。** 兄弟の set_sfaq_scope / add_sfaq_feedback と同じく
        # ACL 付きの get を先に通す。通さないと
        # (a) 他部門の承認済みの判断を、読めない立場から contested に落とせる
        # (b) 「存在しない ID は not found・見えない実在 ID は成功」で存在オラクルになる
        # （ストアでは正しく、束ね層で外れる型）。
        if self.knowledge_store.get(question_id, self.viewer) is None:
            raise ValueError(f"question not found or not visible: {question_id}")
        ok = self.knowledge_store.set_status(question_id, QuestionStatus(status))
        if not ok:
            raise ValueError(f"question not found: {question_id}")
        return {"question_id": question_id, "status": status}

    def raise_lesson_from_task(
        self, task_id: str, lesson_text: str, first_feedback: str | None = None
    ) -> dict:
        """改善タスクから「学び」を起こして SFAQ に載せる（スチュワード専用）。

        申告から派生した改善タスクは**届け先の部門にだけ**見える。約束どおりである代わりに、
        他部門で起きた失敗が部門をまたいで流れる道が無くなる。問いは「その学びは共有したい。
        何と矛盾するのか」である。

        矛盾するのは一点だけで、**いま「学び」と「申告」が同じ1本の文字列だから**である。
        タスクの `evidence` は申告者が書いた本文そのもので、そこには
        「会員名簿をAIに貼る事故が起きうる」（全社の学び）と
        「あの部門の部門長がやった」（当人の話）が混ざっている。そのまま広げれば、
        学びと一緒に誰が誰を告発したかも広がる。

        だからこのツールは **`evidence` を写さない。** スチュワードが自分の言葉で書いた
        `lesson_text` だけを載せる。抽象化するのは人であって、AIでも機械的なコピーでもない
        （配達係原則）。元タスクへの参照は `attributes` に残すので、後から辿ることはできる —
        辿れるのは、その参照を見る権限がある人だけである。
        """
        refusal = self._write_refusal()  # R1
        if refusal is not None:
            return refusal
        if self.knowledge_store is None or self.task_store is None:
            raise RuntimeError("knowledge_store / task_store not configured")
        from waca_loop.improvement.acl import task_visible_to
        from waca_loop.improvement.lesson_guard import (
            borrows_evidence,
            names_in,
            pii_kinds,
        )
        from waca_loop.knowledge.store import new_id
        from waca_loop.models import (
            FbType,
            Feedback,
            Provenance,
            Question,
            QuestionStatus,
        )

        self._require_identity("raise a lesson")
        if STEWARD_UNIT not in self.viewer.units:
            raise PermissionError("only the steward may raise a lesson from a task (§2.4)")

        text = (lesson_text or "").strip()
        if not text:
            raise ValueError("lesson_text is required — 学びは人が書く（写しではない）")

        rows = self.task_store.list(status=None, kind=None, viewer=self.viewer)
        task = next((r for r in rows if r.get("task_id") == task_id), None)
        if task is None or not task_visible_to(task, self.viewer):
            raise ValueError(f"task not found or not visible: {task_id}")

        # 申告の本文が、書いた学びに紛れ込んでいないかを機械的に見る。人が急いで貼り付ける
        # ことは普通にあり、そこを通すとこのツールの意味が無くなる。
        #
        # 検査対象は **同じ申告から派生したタスク全部**の evidence（ひとつの回答から
        # incident / harmful / nps_detractor の3つが立つので、A を指しながら B の本文を
        # 貼る抜け道が残る）、かつ **lesson_text と first_feedback の両方**。
        # first_feedback は埋め込みの主対象なので、見出しだけ一般化しても本文で意味検索に
        # 当たる。
        #
        # **別実装（`evidence_leak` の可変窓）を採らない理由**。
        # あちらは「短い申告ほど窓を狭める」（下限4文字）で、短い申告から一部を抜く形を
        # 捕まえようとする。それは塞ぎすぎの形になる:
        # 「AIを使う前に個人情報が…確認する手順」を共有するだけの**正当な言い換え**まで弾く。
        # 一方、あちらが守ろうとする「氏名だけ抜く」形は、可変窓でも捕まらない（あちらの
        # docstring 自身が「氏名は守備範囲の外」と書いている）。こちらは `names_in` が
        # **名簿で**捕まえる — 氏名は重なり判定ではなく固有表現の問題だから、そちらが正しい道具。
        # `evidence_leak` は純関数として残す（テストが意図を保つ）が、本番の検査経路は
        # `lesson_guard` に一本化する。二つの検査が並ぶと、どちらが効いているか分からなくなる。
        #
        # 裏口（`add_sfaq_feedback`）も含め、両経路とも `_refuse_report_copy` を通る。
        #
        # 突き合わせる evidence は閲覧できるタスクに限られる。スチュワードが見られない
        # 兄弟タスクの本文は corpus から落ちるので、その文を**別経路で手に入れて**貼られると
        # 検査は当たらない。可視範囲を広げるほうが害が大きいので、この残余は受け入れる。
        response_id = task.get("source_response_id")
        evidences = [
            r.get("evidence") or ""
            for r in rows
            if r.get("source_response_id") == response_id and task_visible_to(r, self.viewer)
        ] if response_id else [task.get("evidence") or ""]

        for label, body in (("学びの本文", text), ("フィードバック", first_feedback)):
            if body and borrows_evidence(body, evidences):
                raise ValueError(
                    f"{label}に申告の本文が写されている（部分的な引き写しを含む）。"
                    "共有してよい形（誰の話かが消えた一般化）に書き直してください"
                )
            kinds = pii_kinds(body)
            if kinds:
                raise ValueError(
                    f"{label}に個人を特定できる情報が残っている（{'/'.join(kinds)}）。"
                    "この学びは全社に置かれます。固有名詞・連絡先を外して書き直してください"
                )
            # 氏名は正規表現では決まらないので名簿で見る（lesson_guard.names_in）。
            # person_store が無い構築経路では**素通りさせずに断る** — 氏名ゲートを
            # 欠いたまま org 可視の行を書くくらいなら、書かないほうがよい。
            if self.person_store is None:
                raise RuntimeError(
                    "person_store が無いため氏名の検査ができない。学びの起票を中止した"
                )
            if names_in(body, self.person_store.roster_names()):
                raise ValueError(
                    f"{label}に在籍者の氏名が残っている。"
                    "この学びは全社に置かれます。誰の話かが消えた形に書き直してください"
                )

        # 起点タスクの所有ユニットが全社公開を許さないなら、学びも起こさない。
        # `set_sfaq_scope` は理事会所有の判断を org へ広げることを拒否する。この経路が
        # 起点の ACL を見ずに ORG 固定で作ると、同じ内容が片方のツールでは拒否され、
        # もう片方では通る。作る側にも同じ門を置く。
        from waca_loop.retrieval.acl import widen_refusal

        blocked = widen_refusal(task.get("owner_unit"), ScopeSensitivity.NORMAL)
        if blocked is not None:
            raise PermissionError(
                f"このタスクからは全社の学びを起こせない: {blocked}"
            )

        qid = new_id("q")
        q = Question(
            question_id=qid,
            question_text=text,
            scope=ScopeTags(ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL),
            provenance=Provenance.CORPORATE_TRAINING,
            # 学びはスチュワードが起こしたものなので所有もスチュワード。org 階層なので
            # 判定には使われず、「誰が管理しているか」の記録として残る (§3.3)。
            owner_unit=STEWARD_UNIT,
            attributes={
                # 元タスクへの参照。学びの出どころを辿れるようにするためで、本文は写さない。
                "source_task_id": task_id,
                "raised_by": self.viewer.person_id,
            },
            perm_corporate=True,
            perm_individual=False,
            status=QuestionStatus.PROVISIONAL,   # 起こしただけでは確定ではない (§2.4)
            source="task_lesson",
        )
        self.knowledge_store.add_question(q)
        if first_feedback:
            self.knowledge_store.add_feedback(Feedback(
                feedback_id=new_id("fb"), question_id=qid,
                fb_type=FbType.DECISION, body=first_feedback,
                author_id=self.viewer.person_id,
            ))
        self._trail("lesson_raised", detail=f"{task_id}->{qid}")
        return {"question_id": qid, "source_task_id": task_id, "status": q.status.value}

    def set_sfaq_scope(
        self, question_id: str, tier: str, sensitivity: str | None = None
    ) -> dict:
        """Steward-only rescope — the widening/narrowing step of 生ログ→候補→暫定→承認 (§2.4/§3.3).

        `add_sfaq_question` files everything as `dept`, deliberately: one person's unreviewed
        account should not land org-wide the moment it is typed. This is how a judgment that
        turns out to matter org-wide is widened afterwards (and how one is narrowed).

        Two things are refused, and both are refusals a human would otherwise have to
        remember every single time:

        - **A board-owned row can never go org.** Personnel, contracts, disputes — the one
          unit whose decisions routinely cannot be shared. Opening existing knowledge to
          everyone is the easy path, so the closing has to be structural, not remembered.
        - **A sensitive row can never go org.** Widening the tier while leaving the
          sensitivity axis untouched would produce a row that reads "everyone" on one axis
          and "owning unit only" on another — the kind of disagreement between the two gates
          that §3.3 exists to prevent.

        The refusal carries its reason: a governance tool that fails silently is
        indistinguishable from a broken one.
        """
        refusal = self._write_refusal()  # R1: 組織の業務外セッションは書き換えない
        if refusal is not None:
            return refusal
        if self.knowledge_store is None:
            raise RuntimeError("knowledge_store not configured (SFAQ)")
        from waca_loop.knowledge.store import question_to_row
        from waca_loop.models import ScopeSensitivity, ScopeTier
        from waca_loop.retrieval.acl import widen_refusal

        self._require_identity("change question scope")
        if STEWARD_UNIT not in self.viewer.units:
            raise PermissionError("only the steward may change question scope (§2.4)")

        # `get` is ACL-gated, so an invisible row comes back as None — a steward cannot
        # rescope what they cannot see (no blind writes, §3.3). Gate 2 re-checks in-process,
        # the same redundancy the read path uses.
        from waca_loop.knowledge.acl import question_visible_to

        q = self.knowledge_store.get(question_id, self.viewer)
        if q is None or not question_visible_to(question_to_row(q), self.viewer):
            raise ValueError(f"question not found or not visible: {question_id}")

        new_tier = ScopeTier(tier)
        new_sens = ScopeSensitivity(sensitivity) if sensitivity else None
        if new_tier is ScopeTier.ORG:
            # 判定は **いまの** sensitivity で行う。同じ呼び出しで機微を外しながら全社公開に
            # するのを許すと、「機微を解除した」という判断が公開の陰に隠れて記録されない。
            # 解除は解除として別に一度行い、痕跡を残してから広げること。
            reason = widen_refusal(q.owner_unit, q.scope.sensitivity)
            if reason is not None:
                raise PermissionError(reason)

        ok = self.knowledge_store.set_scope(question_id, new_tier, new_sens, viewer=self.viewer)
        if not ok:
            # 閉じるためのツールが「閉じました」と嘘をつくのが最悪の壊れ方なので、
            # 0行更新は成功として返さない (set_sfaq_status と同じ形)。
            raise ValueError(f"scope not changed (row not found or not writable): {question_id}")
        self._trail(
            "sfaq_rescope",
            detail=(f"{question_id}:{q.scope.tier.value}/{q.scope.sensitivity.value}"
                    f"->{new_tier.value}/{(new_sens or q.scope.sensitivity).value}"),
        )
        return {
            "question_id": question_id,
            "scope_tier": new_tier.value,
            "scope_sensitivity": (new_sens or q.scope.sensitivity).value,
        }


def build_mcp(
    source: ServerContext | Callable[[], ServerContext],
    *,
    name: str = "waca-loop",
    auth_provider: Any | None = None,
    auth_settings: Any | None = None,
    google_callback: Any | None = None,
    host: str | None = None,
    port: int | None = None,
) -> Any:
    """Bind the tools onto a FastMCP server. Lazy-imports ``mcp`` so tests can skip it.

    ``source`` is either a fixed :class:`ServerContext` (stdio admin / tests — one viewer for
    the whole process) or a **zero-arg factory** returning a fresh context per call (hosted http
    mode, where each request carries a different user's verified identity). The factory is invoked
    inside every tool so the per-request viewer drives ACL (§3.3).

    ``auth_provider`` (hosted mode) is the OAuth **authorization server** provider
    (:class:`waca_loop.auth.as_provider.GoogleBrokeringAuthProvider`). Passing it makes FastMCP
    host ``/authorize`` ``/token`` ``/register`` (DCR) and derive a token verifier from the
    provider, so unauthenticated requests are rejected before any tool runs. ``auth_settings``
    (required alongside it) declares the issuer/resource identity + DCR options; ``google_callback``
    is registered as an unauthenticated custom route for the Google redirect. ``None`` everywhere
    (stdio/tests) leaves auth off.

    ``host``/``port`` MUST be passed to the constructor here (not mutated onto ``mcp.settings``
    afterwards): FastMCP decides at construction time whether to auto-enable DNS-rebinding
    protection, and the default host ``127.0.0.1`` bakes in an allowlist of localhost Host
    headers — on Cloud Run every request then dies with ``421 Invalid Host header``.
    """
    from mcp.server.fastmcp import FastMCP

    extra: dict[str, Any] = {}
    if host is not None:
        extra["host"] = host
    if port is not None:
        extra["port"] = port

    if auth_provider is not None:
        mcp = FastMCP(
            name,
            instructions=SYSTEM_PROMPT_RULES,
            auth_server_provider=auth_provider,
            auth=auth_settings,
            **extra,
        )
        if google_callback is not None:
            from waca_loop.auth.as_provider import GOOGLE_APPROVE_PATH, GOOGLE_CALLBACK_PATH

            mcp.custom_route(GOOGLE_CALLBACK_PATH, methods=["GET"])(google_callback)
            approval = getattr(auth_provider, "handle_approval", None)
            if approval is not None:
                mcp.custom_route(GOOGLE_APPROVE_PATH, methods=["POST"])(approval)
    else:
        mcp = FastMCP(name, instructions=SYSTEM_PROMPT_RULES, **extra)

    def _ctx() -> ServerContext:
        return source() if callable(source) else source

    @mcp.tool(
        description=(
            "組織の用語・社内語・略語・人物を解決し、AIの誤読を防ぐ (§4.2)。"
            "k は平常時 20 件が上限（人物は10件）。必要な語だけを引くこと — "
            "大きな k で語彙や名簿をまとめて取り出す使い方はしない（FLOOR-02）。"
            "上限を超える件数がどうしても要る場合のみ reason に業務上の理由を書く"
            "（権限のある利用者だけが超過でき、超過は監査に1行残る。人物は超過不可）。"
        )
    )
    def lookup_terms(query: str, k: int = 5, reason: str | None = None) -> dict:
        return _ctx().lookup_terms(query, k, reason)

    @mcp.tool(
        description=(
            "組織の業務の場合のみ・毎ターン必ず、利用を層1台帳に記録する（要約のみ・逐語送信しない）。"
            "use_category は l2|l1|l0|prohibited（L2=そのまま採用 / L1=条件つき / L0=いまは使わない"
            " / prohibited=禁止列挙に該当）、org_context に組織との関連を、policy_row_ids に"
            "get_applicable_policy の該当行IDを渡す。他組織・私事は記録禁止 — set_session_scope で"
            "external/private を宣言し、帰属が曖昧ならユーザーに確認。"
            "request_id は**この1件の記録の再送キー**（任意・id 形のみ）— 書き込みが"
            "WACA_LOOP_LEDGER_WRITE_FAILED で失敗したときに同じ値で再送すると二重記録にならない。"
            "**ターンをまたいで同じ値を使い回さないこと**（別の記録が再送とみなされる）。"
        )
    )
    def log_usage(
        use_category: str,
        data_kinds: list[str],
        summary: str,
        client: str,
        request_id: str | None = None,
        policy_row_ids: list[str] | None = None,
        org_context: str | None = None,
    ) -> dict:
        return _ctx().log_usage(
            use_category, data_kinds, summary, client, request_id, policy_row_ids, org_context
        )

    @mcp.tool(
        description=(
            "この利用者の組織帰属を宣言する（scope: org|external|private|unknown）。"
            "組織の業務と無関係な会話（他組織・大学業務・私事）では冒頭で external/private を宣言"
            "すること — 以降WACA Loopへの記録はサーバー側で拒否される。"
            "**宣言は会話をまたいで残り、org で宣言し直すまで解除されない** — "
            "組織の業務に戻ったら scope=\"org\" を宣言すること（しないと記録が落ち続ける）。"
        )
    )
    def set_session_scope(scope: str, reason_summary: str | None = None) -> dict:
        return _ctx().set_session_scope(scope, reason_summary)

    @mcp.tool(
        description=(
            "保留(pending)になった台帳記録をスチュワードが審査する。"
            "approve=正式記録 / discard=完全削除（スチュワード専用）。"
        )
    )
    def review_pending_usage(log_id: str, action: str) -> dict:
        return _ctx().review_pending_usage(log_id, action)

    @mcp.tool(
        description=(
            "自分の台帳記録を取り下げる（R6）。私事・他組織の内容がうっかり記録された"
            "ことに気づいたら、その log_id を渡す。取り下げた記録は通常の読み取り・集計"
            "から外れる。reason は private|other_org|mistake のいずれか（自由文は不可 — "
            "取り下げたい内容をここに書き写さないため）。**自分の記録しか取り下げられない**。"
        )
    )
    def request_log_deletion(log_id: str, reason: str) -> dict:
        return _ctx().request_log_deletion(log_id, reason)

    @mcp.tool(
        description=(
            "取り下げ済みの台帳記録を一覧する（スチュワード専用・R6）。"
            "インシデント調査など、必要があるときだけ使う。"
        )
    )
    def list_withdrawn_usage(limit: int | None = None) -> dict:
        return _ctx().list_withdrawn_usage(limit)

    @mcp.tool(
        description=(
            "取り下げ済みの台帳記録を**1件だけ**、要約つきで読む（スチュワード専用・R6）。"
            "インシデント調査など、読む理由があるときだけ使う。reason は必須だが"
            "**監査には残さない**（自由文を台帳へ写さないため）。監査に残るのは"
            "「誰がどの log_id を読んだか」。"
        )
    )
    def get_withdrawn_usage(log_id: str, reason: str) -> dict:
        return _ctx().get_withdrawn_usage(log_id, reason)

    @mcp.tool(
        description=(
            "個人情報を含みそうなデータ（名簿・会員リスト等）の処理依頼を受けたら、処理前に"
            "必ずこれを呼ぶ。**列名のみ**を渡す（実際の値は絶対に送らない — 値らしき文字列は"
            "サーバ側で拒否される）。purpose 未指定なら"
            "検知結果+利用目的の選択肢（A集計/Bテスト/C突合/D名簿/Eその他）が返るのでユーザーに"
            "確認し、purpose 指定で再呼び出しすると匿名化仕様書が返る。仕様の実行は範囲外。"
        )
    )
    def get_pii_anonymization_guidance(
        columns: list[str], purpose: str | None = None
    ) -> dict:
        return _ctx().get_pii_anonymization_guidance(columns, purpose)

    @mcp.tool(description="用語＋直近の関連情報をまとめて返す薄いラッパ (§4.2)。")
    def get_company_context(query: str) -> dict:
        return _ctx().get_company_context(query)

    @mcp.tool(
        description=(
            "本人のAI活用状況から、安全に試せる次の一歩を返す（Lite）。"
            "task_hint_summary は要約のみ。raw prompt や逐語の入力は渡さない。"
        )
    )
    def get_personal_coach(
        task_hint_summary: str | None = None, max_items: int = 3
    ) -> dict:
        return _ctx().get_personal_coach(task_hint_summary, max_items)

    @mcp.tool(
        description=(
            "本人向け推薦を定期出力用の構造化行として返す（Lite）。"
            "このツール自体はSheetsへ書き込まない。"
        )
    )
    def export_personal_recommendations(max_items: int = 3) -> dict:
        return _ctx().export_personal_recommendations(max_items)

    @mcp.tool(
        description=(
            "直近の要約済みAI利用ログとポリシーから、現在地・あるべき姿・今日作る成果物を返し、"
            "対話を始める。goal_hint_summary は要約のみ。raw prompt や逐語は渡さない。"
            "下限は内部判定として使い、触れそうな時だけ今回の業務に沿って短く返す。"
        )
    )
    def start_policy_workbench(
        goal_hint_summary: str | None = None,
        artifact_type: str | None = None,
        max_logs: int = 20,
    ) -> dict:
        return _ctx().start_policy_workbench(goal_hint_summary, artifact_type, max_logs)

    @mcp.tool(description="定期アンケートを返す。なければ none（スキップ可）。")
    def get_pending_survey() -> dict:
        return _ctx().get_pending_survey()

    @mcp.tool(
        description=(
            "アンケート回答を記録（要約のみ）。NPS・有用/有害・インシデント。"
            "内容を特定し改善課題を起票。"
            "届け先は所属部門の単位: 所属部門が一意ならその部門、複数所属・所属不明・"
            "所属外のユニットを指定した場合は**スチュワード（ガバナンス担当）**に届く。"
            "複数部門に所属している回答者は owner_unit で届け先を選べる。"
            "返り値の owner_unit が実際の届け先なので、機微な申告のときは"
            "**先に誰が見るのかを回答者に伝え、それでよいか確認してから送ること**。"
            "自部門に知られたくない申告（当事者が同じ部門にいる等）は escalate=true を"
            "指定すると自部門を飛ばし、スチュワード（およびスチュワード範囲の閲覧権を"
            "持つ人）に届く。escalate を付けた申告は機微として扱われる。"
            "スチュワード自身に関する申告はこのツールでは扱えない — その場合は記録せず、"
            "人の窓口（AIポリシー承認者）に直接相談するよう案内すること。"
            "組織の業務外の内容は記録禁止。迷ったらユーザーに確認。"
        )
    )
    def submit_survey_response(
        nps_score: int,
        nps_reason: str,
        useful: str,
        harmful: str,
        incident_flag: bool,
        incident_summary: str | None = None,
        contact_opt_in: bool = False,
        ref_log_id: str | None = None,
        ref_request_id: str | None = None,
        campaign_id: str | None = None,
        owner_unit: str | None = None,
        escalate: bool = False,
    ) -> dict:
        return _ctx().submit_survey_response(
            nps_score, nps_reason, useful, harmful, incident_flag,
            incident_summary, contact_opt_in, ref_log_id, ref_request_id,
            campaign_id, owner_unit, escalate,
        )

    @mcp.tool(
        description=(
            "プロンプト/入力から抽出した用語・SFAQ・ERN素データ候補を共有範囲つきで提案・記録"
            "（AI提案）。既定は提案者の部門スコープ(dept)。owner_unit は提案者が"
            "所属するユニットのみ指定でき、全社(org)スコープの提案はスチュワード限定。"
            "組織の業務外の内容は記録禁止。迷ったらユーザーに確認。"
        )
    )
    def propose_candidates(items: list[dict]) -> dict:
        return _ctx().propose_candidates(items)

    @mcp.tool(
        description=(
            "抽出候補を承認/却下。承認した用語は用語層へ昇格。"
            "用語・SFAQ いずれの昇格も**スチュワード限定**(全員のAIが参照する層を書き換える"
            "ため・SPEC_phase2 §2.4)。SFAQ 候補の承認は provisional の問いを作る"
            "(payload 規約: question_text/judgment/fb_type/provenance)。"
        )
    )
    def approve_candidate(
        candidate_id: str, approve: bool = True, edits: dict | None = None
    ) -> dict:
        return _ctx().approve_candidate(candidate_id, approve, edits)

    @mcp.tool(
        description=(
            "WACA Loop 自身の不具合・改善要望を記録する（SPEC_improvement_requests §3.1）。"
            "kind は bug|improvement|other。**利用者が明示的に要望を述べたときだけ呼ぶ** —"
            "会話から自動で拾わない（SFAQ の判断ナレッジと混ざるため §9.2）。"
            "anonymous=True にすると起案者を残さない（事後に取り消せない）。"
        )
    )
    def report_improvement_request(
        kind: str,
        title: str,
        body: str,
        anonymous: bool = False,
        owner_unit: str | None = None,
    ) -> dict:
        return _ctx().report_improvement_request(kind, title, body, anonymous, owner_unit)

    @mcp.tool(
        description=(
            "未対応の改善要望の一覧（既定は open のみ）。WACA Loop の開発を始めるときに最初に見る"
            "(SPEC_improvement_requests §4)。"
        )
    )
    def list_improvement_requests(status: str | None = None, kind: str | None = None) -> dict:
        return _ctx().list_improvement_requests(status, kind)

    @mcp.tool(
        description=(
            "改善要望の状態を進める（open|designing|implemented|released|wontfix）。"
            "**wontfix(見送り)は WACA Loop オーナー限定** (§8)。resolution に実装内容の要約を書くと"
            "起案者向けのシートに反映される。"
        )
    )
    def set_request_status(
        request_id: str, status: str, resolution: str | None = None
    ) -> dict:
        return _ctx().set_request_status(request_id, status, resolution)

    @mcp.tool(description="改善課題の一覧（管理者向け）。")
    def list_improvement_tasks(status: str | None = None, kind: str | None = None) -> dict:
        return _ctx().list_improvement_tasks(status, kind)

    @mcp.tool(
        description=(
            "この部署・作業に効くAIポリシーを返す（採用レベル2=そのまま/1=条件付き/0=未使用、"
            "禁止床=迂回路つき）。AI利用の前に確認 (SPEC_phase1 §9.2)。"
            "coverage_hint が返ったら、その1行をユーザに伝えること"
            "（自分の記録の取りこぼしについての助言。利用は止めない）。"
        )
    )
    def get_applicable_policy(task_hint: str | None = None) -> dict:
        return _ctx().get_applicable_policy(task_hint)

    @mcp.tool(
        description=(
            "その領域に詳しい社内の人を最大3名返す（ERN・読み取り専用）。"
            "「誰に聞けばいいか」を尋ねられたときに使う。"
            "**順位・点数・件数は返らない**。返るのは帯（近い/やや近い、最近/しばらく前）と、"
            "根拠が実績ベースか役職ベースかだけ。並びは person_id 順で、良い順ではない。"
            "本人には通知されない。候補は同意している在籍者に限られる。"
            "この結果で人を評価・査定してはならない（減点利用は禁止）。"
        )
    )
    def find_expert(topic: str, k: int = 3) -> dict:
        return _ctx().find_expert(topic, k)

    @mcp.tool(
        description=(
            "過去の「問い」と組織の判断・理由を検索して返す（SFAQ・読み取り専用）。"
            "判断・方針・経緯に関する質問はまずこれで過去の判断を参照する。"
            "status=provisional/contested は未確定・係争中として注意書きつきで扱うこと。"
            "k は平常時 20 件が上限。手元の問いに答えるのに要る分だけを引く（FLOOR-02）。"
            "上限を超える件数がどうしても要る場合のみ reason に業務上の理由を書く"
            "（権限のある利用者だけが超過でき、超過は監査に1行残る）。"
        )
    )
    def search_sfaq(query: str, k: int = 5, reason: str | None = None) -> dict:
        return _ctx().search_sfaq(query, k, reason)

    @mcp.tool(
        description=(
            "問いへのフィードバックを記録（書き込み）。fb_type は AI が判断して付与: "
            "decision(こう決めた)|agree(同意)|refute(それは違う)|supplement(補足)|"
            "open(未解決の論点)|useful(役立った)。記録前に解釈を復唱しユーザ確認を得ること"
            "（復唱→確認→感謝, フィードバックB）。"
            "組織の業務外の内容は記録禁止。迷ったらユーザーに確認。"
        )
    )
    def add_sfaq_feedback(question_id: str, fb_type: str, body: str) -> dict:
        return _ctx().add_sfaq_feedback(question_id, fb_type, body)

    @mcp.tool(
        description=(
            "新しい「問い」を候補(candidate)として起票（書き込み）。"
            "会話で価値ある判断・論点が生まれ、ユーザが共有に同意したときに使う。"
            "起票者の所属部門スコープで記録される（全社公開ではない）。複数部門に所属している"
            "場合のみ owner_unit を指定する（エラーメッセージが選択肢を示す）。"
            "組織の業務外の内容は記録禁止。迷ったらユーザーに確認。"
        )
    )
    def add_sfaq_question(
        question_text: str,
        first_feedback: str | None = None,
        owner_unit: str | None = None,
    ) -> dict:
        return _ctx().add_sfaq_question(question_text, first_feedback, owner_unit)

    @mcp.tool(
        description="問いの状態遷移（approved/contested 等）。スチュワード専用（§2.4 承認は人）。"
    )
    def set_sfaq_status(question_id: str, status: str) -> dict:
        return _ctx().set_sfaq_status(question_id, status)

    @mcp.tool(
        description=(
            "スチュワード専用: 判断の公開範囲を変える(§3.3)。tier は org/dept/team/personal。"
            "理事会所有の行と機微(sensitive)の行は org にできない — 理由付きで拒否される。"
        )
    )
    def set_sfaq_scope(question_id: str, tier: str, sensitivity: str | None = None) -> dict:
        return _ctx().set_sfaq_scope(question_id, tier, sensitivity)

    @mcp.tool(
        description=(
            "スチュワード専用: 改善タスクから「学び」を起こして SFAQ に載せる。"
            "申告の本文は写さない — 共有してよい形に人が書き直したものだけを載せる。"
            "元タスクへの参照は残るので後から辿れる。"
        )
    )
    def raise_lesson_from_task(
        task_id: str, lesson_text: str, first_feedback: str | None = None
    ) -> dict:
        return _ctx().raise_lesson_from_task(task_id, lesson_text, first_feedback)

    return mcp


@dataclass
class SharedRuntime:
    """Process-lifetime singletons shared across all requests (hosted http mode).

    The BigQuery client + stores are **viewer-independent** and built once; only the
    viewer-bound ``TermStore`` is constructed per request via :attr:`term_store_factory`
    (the BigQuery store bakes the viewer into its ACL WHERE clause, §3.3). This is what lets
    one server serve many users from one service-account BigQuery connection while resolving
    identity — and therefore ACL — per request.
    """

    term_store_factory: Callable[[Viewer], TermStore]
    usage_store: _UsageStore
    embedder: Embedder
    persons: Any                # BigQueryPersonStore: search() + resolve_person_id()/get()
    survey_store: Any | None = None
    task_store: Any | None = None
    request_store: Any | None = None
    request_sheet_mirror: Any | None = None   # §10 即時反映（未設定なら何もしない）
    candidate_store: Any | None = None
    policy_store: Any | None = None
    policy_guide_store: Any | None = None
    adoption_map_store: Any | None = None
    # SFAQ store is viewer-bound (Gate 1 bakes the viewer into its SQL WHERE), so a
    # factory like term_store_factory — not a shared instance.
    knowledge_store_factory: Callable[[Viewer], Any] | None = None
    attribution: str = "pseudonymous"
    cadence: str = "monthly"
    # WACA Loop ops guards (SPEC_ops P0) — process-wide, viewer-independent singletons.
    scope_registry: Any | None = None       # SessionScopeRegistry (R1)
    tool_event_store: Any | None = None     # tool_events trail (R3a)
    org_guard_strict: bool = False          # R2 rollout flag
    export_admin_units: frozenset[str] = frozenset()   # FLOOR-02 break-glass holders
    # ERN. assertions は viewer をメソッド引数で受ける（persons と同じ形）ので
    # 共有インスタンスでよい。audit は書き込み専用（読み出すツールを作らない・§4f）。
    ern_assertion_store: Any | None = None
    ern_audit_store: Any | None = None
    ern_half_life_days: int = 180
    ern_role_fallback: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # OAuth DCR client registry (hosted mode) — persisted so connector registrations survive
    # instance restarts (BigQueryOAuthClientStore in production).
    oauth_client_store: Any | None = None


def build_shared_runtime(*, credentials: Any | None = None) -> SharedRuntime:
    """Build the viewer-independent BigQuery runtime once (§5).

    ``credentials=None`` → Application Default Credentials (the Cloud Run service account);
    the server becomes the sole BigQuery gatekeeper so the ACL invariant (§3.3) is meaningful.
    Pass the admin's OAuth creds for the local stdio path (``bq_auth=user_oauth``).
    """
    from waca_loop.auth.client_store import BigQueryOAuthClientStore
    from waca_loop.bq import make_client
    from waca_loop.config import get_settings
    from waca_loop.embeddings import build_embedder
    from waca_loop.ern.store import BigQueryErnAssertionStore, BigQueryErnAuditStore
    from waca_loop.grounding.persons import BigQueryPersonStore
    from waca_loop.grounding.terms import BigQueryTermStore
    from waca_loop.guard import SessionScopeRegistry
    from waca_loop.improvement.candidates import BigQueryCandidateStore
    from waca_loop.improvement.requests import BigQueryRequestStore
    from waca_loop.improvement.sheet_mirror import build_mirror
    from waca_loop.improvement.tasks import BigQueryTaskStore
    from waca_loop.knowledge.store import BigQueryQuestionStore
    from waca_loop.ledger.session_scopes import BigQuerySessionScopeStore
    from waca_loop.ledger.tool_events import BigQueryToolEventStore
    from waca_loop.ledger.usage_log import BigQueryUsageLogStore
    from waca_loop.policy.sheets import LiteSheetsPolicyStore
    from waca_loop.policy.store import BigQueryPolicyStore
    from waca_loop.survey.store import BigQuerySurveyStore

    settings = get_settings()
    client = make_client(settings, credentials=credentials)
    embedder = build_embedder(settings.embedding_provider, dim=settings.embedding_dim)
    persons = BigQueryPersonStore(client, settings)

    def term_store_factory(viewer: Viewer) -> TermStore:
        return BigQueryTermStore(client, settings, viewer, embedder)

    def knowledge_store_factory(viewer: Viewer) -> BigQueryQuestionStore:
        return BigQueryQuestionStore(client, settings, viewer, embedder)

    lite_policy_store = None
    if settings.lite_policy_store_json:
        try:
            lite_policy_store = LiteSheetsPolicyStore.from_json_file(
                settings.lite_policy_store_json,
                workspace_domains=settings.workspace_domains_set,
            )
        except Exception:  # noqa: BLE001 — Lite policy だけの誤記で MCP 全体を落とさない
            logger.warning("waca-loop: Lite policy JSON load failed; store left unconfigured")

    return SharedRuntime(
        term_store_factory=term_store_factory,
        usage_store=BigQueryUsageLogStore(client, settings),
        embedder=embedder,
        persons=persons,
        survey_store=BigQuerySurveyStore(client, settings),
        task_store=BigQueryTaskStore(client, settings),
        request_store=BigQueryRequestStore(client, settings),
        # §10 即時反映。シート ID 未設定なら None で、`_mirror_requests_to_sheet` は何もしない。
        request_sheet_mirror=build_mirror(settings),
        candidate_store=BigQueryCandidateStore(client, settings),
        policy_store=BigQueryPolicyStore(client, settings),
        policy_guide_store=lite_policy_store,
        adoption_map_store=lite_policy_store,
        knowledge_store_factory=knowledge_store_factory,
        attribution=settings.survey_attribution,
        cadence=settings.survey_cadence,
        # Persisted + TTL'd: a declaration survives a restart, and the ONLY thing that ends it
        # is re-declaring or the TTL lapsing. Without the store, every deploy would silently
        # reset everyone to `unknown` — an uncontrolled expiry in the fail-open direction.
        scope_registry=SessionScopeRegistry(
            store=BigQuerySessionScopeStore(client, settings),
            ttl_hours=settings.scope_ttl_hours,
        ),
        tool_event_store=BigQueryToolEventStore(client, settings),
        org_guard_strict=settings.org_guard_strict,
        export_admin_units=settings.export_admin_units_set,
        ern_assertion_store=BigQueryErnAssertionStore(client, settings),
        ern_audit_store=BigQueryErnAuditStore(client, settings),
        # E9: 半減期は config 由来。ロジック内リテラルにしない。
        ern_half_life_days=settings.ern_half_life_days,
        oauth_client_store=BigQueryOAuthClientStore(client, settings),
    )


def context_for(viewer: Viewer, runtime: SharedRuntime) -> ServerContext:
    """Assemble a per-request :class:`ServerContext` for ``viewer`` from shared singletons."""
    return ServerContext(
        term_store=runtime.term_store_factory(viewer),
        usage_store=runtime.usage_store,
        embedder=runtime.embedder,
        viewer=viewer,
        person_store=runtime.persons,
        survey_store=runtime.survey_store,
        task_store=runtime.task_store,
        request_store=runtime.request_store,
        request_sheet_mirror=runtime.request_sheet_mirror,
        candidate_store=runtime.candidate_store,
        policy_store=runtime.policy_store,
        policy_guide_store=runtime.policy_guide_store,
        adoption_map_store=runtime.adoption_map_store,
        knowledge_store=(
            runtime.knowledge_store_factory(viewer)
            if runtime.knowledge_store_factory is not None
            else None
        ),
        attribution=runtime.attribution,
        cadence=runtime.cadence,
        scope_registry=runtime.scope_registry,
        tool_event_store=runtime.tool_event_store,
        org_guard_strict=runtime.org_guard_strict,
        export_admin_units=runtime.export_admin_units,
        ern_assertion_store=runtime.ern_assertion_store,
        ern_audit_store=runtime.ern_audit_store,
        ern_half_life_days=runtime.ern_half_life_days,
        ern_role_fallback=runtime.ern_role_fallback,
    )


def build_production_context() -> ServerContext:
    """Wire a single-viewer context for the local stdio admin path (§5).

    The running admin authenticates interactively; their OAuth creds drive BigQuery
    (``bq_auth=user_oauth``) and their identity fixes the one viewer for the process.
    """
    from waca_loop.auth.identity import resolve_viewer
    from waca_loop.auth.oauth import OAuthManager
    from waca_loop.config import get_settings

    settings = get_settings()
    oauth = OAuthManager(settings)
    creds = oauth.credentials() if settings.bq_auth == "user_oauth" else None
    runtime = build_shared_runtime(credentials=creds)
    identity = oauth.identity()
    if runtime.persons.resolve_person_id(identity.subject) is None:
        # Same first-login binding as the hosted server (auth/as_provider.py): a person
        # registered ahead of time by email (scripts/register_members.py) needs no admin step.
        from waca_loop.auth.identity import claim_preregistration

        claim_preregistration(identity, runtime.persons, settings.workspace_domains_set)
    viewer = resolve_viewer(identity, runtime.persons)
    return context_for(viewer, runtime)


def _current_subject() -> str:
    """The verified OAuth ``sub`` of the caller of the in-flight http request (hosted mode).

    Reads the token validated by the MCP auth middleware (§5). Raises if unauthenticated so a
    request without a valid identity never reaches a store (no anonymous BigQuery exposure).
    """
    from mcp.server.auth.middleware.auth_context import get_access_token

    token = get_access_token()
    if token is None or not getattr(token, "subject", None):
        raise PermissionError("unauthenticated MCP request (no verified subject)")
    return token.subject


def build_request_context_factory(runtime: SharedRuntime) -> Callable[[], ServerContext]:
    """Return a zero-arg factory that resolves the *current request's* viewer → context (§3.3).

    Fails closed on an unresolvable subject: hosted tokens are only ever minted for
    ``oauth_identity_map`` members ("unregistered sub → rejected, never a visitor"), so a
    verified token whose sub no longer resolves means the person was *removed* from the
    allowlist after issuance. They must be denied outright, not downgraded to an anonymous
    viewer that could still read org-public rows and write ledger entries.
    """
    from waca_loop.auth.identity import resolve_viewer
    from waca_loop.auth.oauth import Identity

    def factory() -> ServerContext:
        identity = Identity(subject=_current_subject())
        viewer = resolve_viewer(identity, runtime.persons)
        if viewer.person_id is None:
            raise PermissionError(
                "subject is no longer registered in oauth_identity_map (SPEC_phase1 §5 gate)"
            )
        return context_for(viewer, runtime)

    return factory


def main() -> None:
    """Console entry point (``waca-loop``): run the MCP server over stdio (local admin)."""
    from waca_loop.org import exit_if_incomplete

    exit_if_incomplete()
    context = build_production_context()
    mcp = build_mcp(context)
    mcp.run()


def serve_http() -> None:
    """Console entry point (``waca-loop-serve``): hosted remote MCP over Streamable HTTP.

    One service-account BigQuery connection; identity (and thus ACL) is resolved **per request**
    from the caller's verified token. Deployed on Cloud Run (docs/SETUP_gcp.md §8). The server is
    its
    own OAuth authorization server, brokering Google login behind DCR — see
    :mod:`waca_loop.auth.as_provider`.

    **Fails closed at startup.** Because the server holds a single service-account BigQuery
    connection and is the sole gatekeeper (§3.3), it must not be exposed without authentication.
    Without ``MCP_PUBLIC_URL`` there is no issuer/resource identifier for the OAuth metadata, so
    this refuses to start rather than serve anonymous callers.
    """
    from waca_loop.auth.as_provider import GoogleBrokeringAuthProvider, build_auth_settings
    from waca_loop.config import get_settings
    from waca_loop.org import exit_if_incomplete

    exit_if_incomplete()
    settings = get_settings()
    if not settings.mcp_public_url:
        raise RuntimeError(
            "Refusing to start an UNAUTHENTICATED public MCP server. Set MCP_PUBLIC_URL "
            "(the issuer + resource identifier for the OAuth authorization-server metadata) "
            "before exposing waca-loop-serve (§3.3 / §5)."
        )
    if not settings.mcp_token_signing_key:
        raise RuntimeError(
            "Refusing to start without MCP_TOKEN_SIGNING_KEY. The AS mints self-contained "
            "HMAC-signed tokens (auth/tokens.py) so connections survive instance restarts; "
            "without the key no token could ever be verified. Set it from Secret Manager "
            "(docs/SETUP_gcp.md §8.3)."
        )

    runtime = build_shared_runtime()  # ADC / service account
    provider = GoogleBrokeringAuthProvider(
        settings,
        runtime.persons,
        client_store=runtime.oauth_client_store,
        signing_key=settings.mcp_token_signing_key,
    )
    mcp = build_mcp(
        build_request_context_factory(runtime),
        auth_provider=provider,
        auth_settings=build_auth_settings(settings),
        google_callback=provider.handle_google_callback,
        # host/port go through the constructor — see build_mcp docstring (421 Invalid Host).
        host="0.0.0.0",  # noqa: S104 — Cloud Run requires binding all interfaces
        port=settings.port,
    )
    # Same thing ``mcp.run(transport="streamable-http")`` does, with the two connector-facing
    # middlewares wrapped around it (metadata parity + access log). FastMCP appends custom routes
    # AFTER its own, so the OAuth metadata paths cannot be overridden with `custom_route` — the
    # wrapping has to happen out here. See waca_loop.auth.asgi (tests: C5–C7).
    import uvicorn

    from waca_loop.auth.asgi import build_http_app

    uvicorn.run(
        build_http_app(mcp),
        host="0.0.0.0",  # noqa: S104 — Cloud Run requires binding all interfaces
        port=settings.port,
        log_level=mcp.settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
