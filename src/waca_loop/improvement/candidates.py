"""Extraction candidates — store + approval.

The AI extracts term/SFAQ/ERN candidates from prompts/input and proposes a sharing scope
(hierarchy + circles). Nothing reaches the grounding layer until a user approves
(AI提案＋利用者承認). On approval a TERM candidate is promoted into the term store; SFAQ/ERN
payloads are retained for their SFAQ / ERN pipelines.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from waca_loop.models import (
    CandidateKind,
    CandidateStatus,
    ExtractionCandidate,
    ScopeAudience,
    ScopeTags,
    Term,
)

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _candidate_to_row(c: ExtractionCandidate, candidate_id: str, created_at: datetime) -> dict:
    import json

    return {
        "candidate_id": candidate_id,
        "created_at": created_at.isoformat(),
        "updated_at": created_at.isoformat(),
        "kind": c.kind.value,
        "source": c.source,
        "source_ref": c.source_ref,
        "payload": json.dumps(c.payload, ensure_ascii=False),
        "scope_tier": c.proposed_scope.tier.value,
        "scope_audience": c.proposed_scope.audience.value,
        "scope_sensitivity": c.proposed_scope.sensitivity.value,
        "proposed_circles": list(c.proposed_circles),
        "owner_unit": c.owner_unit,
        "status": c.status.value,
    }


def _decode_payload(raw: object) -> dict:
    """BigQuery の JSON 列から payload を dict として取り出す。

    書き込み側が ``json.dumps`` した文字列を JSON 列に入れているため、BigQuery が
    「JSON としての文字列」を保持し、``TO_JSON_STRING`` が**二重にエンコードされた**値を
    返すことがある。その場合 1回の ``json.loads`` では str のままで、承認時の
    ``{**candidate.payload, ...}`` が BigQuery でだけ落ちる（オフラインは dict を持ち回るので
    テストでは気づけない）。dict になるまで、高々2回まで解く。

    **解けなければ落とす。`{}` を返して先へ進めない。** 空の dict を返すと、壊れた候補が
    「中身の無い正当な候補」に化け、TERM の承認経路が `Term(term="", definition="")` を
    共有語彙へ書き込む — 壊れたデータが**承認済みの組織知識**になる。読めないものは読めないと言う。
    """
    import json

    value = raw
    for _ in range(2):
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"candidate payload is not valid JSON: {exc}") from exc
    if isinstance(value, dict):
        return value   # 2回目の loads で dict になった場合（ループを抜けた直後）
    raise ValueError(
        f"candidate payload decoded to {type(value).__name__}, expected a JSON object"
    )


class InMemoryCandidateStore:
    """In-memory candidate store (tests / offline). Keeps objects so approval can mutate them."""

    def __init__(self) -> None:
        self._by_id: dict[str, ExtractionCandidate] = {}

    def add(self, candidate: ExtractionCandidate) -> str:
        candidate_id = str(uuid.uuid4())
        candidate.candidate_id = candidate_id
        self._by_id[candidate_id] = candidate
        return candidate_id

    def get(self, candidate_id: str) -> ExtractionCandidate | None:
        return self._by_id.get(candidate_id)

    def set_status(self, candidate_id: str, status: CandidateStatus) -> None:
        c = self._by_id.get(candidate_id)
        if c is not None:
            c.status = status

    def list(self, *, status: str | None = None) -> list[ExtractionCandidate]:
        out = list(self._by_id.values())
        return [c for c in out if status is None or c.status.value == status]


class BigQueryCandidateStore:
    """Production candidate store backed by BigQuery."""

    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def add(self, candidate: ExtractionCandidate) -> str:
        candidate_id = str(uuid.uuid4())
        candidate.candidate_id = candidate_id
        row = _candidate_to_row(candidate, candidate_id, _utcnow())
        errors = self._client.insert_rows_json(self._settings.extraction_candidates_table, [row])
        if errors:
            raise RuntimeError(f"extraction_candidates insert failed: {errors}")
        return candidate_id

    def set_status(self, candidate_id: str, status: CandidateStatus) -> None:
        from google.cloud import bigquery

        sql = (
            f"UPDATE `{self._settings.extraction_candidates_table}` "
            "SET status = @s, updated_at = CURRENT_TIMESTAMP() WHERE candidate_id = @id"
        )
        self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("s", "STRING", status.value),
                    bigquery.ScalarQueryParameter("id", "STRING", candidate_id),
                ]
            ),
        ).result()

    def get(self, candidate_id: str) -> ExtractionCandidate | None:
        """Fetch one candidate by id (store-pair twin of InMemoryCandidateStore.get).

        No viewer clause on purpose: approval authority is enforced above this layer
        (**every** kind's approval is steward-only in ServerContext.approve_candidate), and the
        steward reviews candidates across units
        by design. That justification only holds while that gate keys on the candidate
        kind rather than on which sink happens to be wired. The BigQuery audit link for
        a promoted candidate is ``questions.attributes.candidate_id`` (reverse link) —
        ``payload.promoted_question_id`` is only persisted by the in-memory store.
        """

        from google.cloud import bigquery

        from waca_loop.models import (
            ScopeAudience,
            ScopeSensitivity,
            ScopeTags,
            ScopeTier,
        )

        sql = (
            "SELECT candidate_id, kind, source, source_ref, TO_JSON_STRING(payload) AS payload, "
            "scope_tier, scope_audience, scope_sensitivity, proposed_circles, owner_unit, status "
            f"FROM `{self._settings.extraction_candidates_table}` WHERE candidate_id = @id"
        )
        rows = list(
            self._client.query(
                sql,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("id", "STRING", candidate_id)
                    ]
                ),
            ).result()
        )
        if not rows:
            return None
        r = dict(rows[0])
        payload = r.get("payload")
        return ExtractionCandidate(
            kind=CandidateKind(r["kind"]),
            payload=_decode_payload(payload),
            proposed_scope=ScopeTags(
                ScopeTier(r["scope_tier"]),
                ScopeAudience(r["scope_audience"]),
                ScopeSensitivity(r["scope_sensitivity"]),
            ),
            candidate_id=r["candidate_id"],
            source=r["source"],
            source_ref=r.get("source_ref"),
            proposed_circles=list(r.get("proposed_circles") or []),
            owner_unit=r.get("owner_unit"),
            status=CandidateStatus(r["status"]),
        )


def _term_id_for(candidate_id: str) -> str:
    """承認された用語候補の `term_id`。**候補 id から決まる**.

    UUID だと、シンクへの書き込みと `set_status` のあいだで落ちた再試行が、同じ語を
    別 id で二度入れる。BigQuery に PK は無く、`add` も存在確認をしないので、
    `lookup_terms` がその語を永久に2回返し、機械的にどちらかを選ぶ手立てが無くなる。
    SFAQ の問いの id（`stable_id`）と同じ手を、ここでも使う。
    """
    digest = hashlib.sha256(f"candidate:{candidate_id}".encode()).hexdigest()[:16]
    return f"t_h{digest}"


def approve_candidate(
    candidate: ExtractionCandidate,
    approve: bool,
    *,
    term_sink: Any | None = None,
    knowledge_sink: Any | None = None,
    approver_id: str | None = None,
    edits: dict | None = None,
) -> CandidateStatus:
    """Apply a user's approval decision (AI提案＋利用者承認).

    On approve: a TERM candidate is promoted into ``term_sink`` (the term store) as an
    approved Term carrying the proposed scope/owner_unit; an SFAQ candidate is promoted
    into ``knowledge_sink`` (the SFAQ store) as a *provisional* Question — the approval
    itself is the human gate (§2.4), so the row goes live with its 暫定 label. The
    candidate's ``proposed_scope`` is carried verbatim onto the promoted row (tier and
    sensitivity; ``audience`` is pinned to internal on both branches). That is safe only
    because ``propose_candidates`` validates the scope at proposal time — dept by default,
    ``owner_unit`` restricted to the proposer's own units, org tier steward-only (§3.3).
    ERN candidates are marked approved and left for the ERN pipeline.
    Returns the new status.
    """
    if not approve:
        candidate.status = CandidateStatus.REJECTED
        return candidate.status

    # The rescope guard has to hold at EVERY point a row can reach the org tier, not only at
    # `set_sfaq_scope`. Promotion carries `proposed_scope` verbatim onto the live row, so
    # without this a board-owned org-tier candidate would walk straight past the door that
    # tool closes — "a door next to an open window".
    from waca_loop.models import ScopeTier as _ScopeTier

    if candidate.proposed_scope.tier is _ScopeTier.ORG:
        from waca_loop.retrieval.acl import widen_refusal

        reason = widen_refusal(candidate.owner_unit, candidate.proposed_scope.sensitivity)
        if reason is not None:
            raise PermissionError(reason)

    payload = {**candidate.payload, **(edits or {})}
    if candidate.kind == CandidateKind.TERM and term_sink is not None:
        # Idempotent, for the same reason `promoted_question_id` exists on the SFAQ branch:
        # the sink write and the `set_status` write are two calls, and the term write mints
        # a row, so a retry between them must not mint a second one.
        #
        # **`promoted_term_id` alone does not close that window on BigQuery**.
        # `BigQueryCandidateStore.set_status` writes
        # only `status` / `updated_at`, and its own `get` docstring says so:
        # "``payload.promoted_question_id`` is only persisted by the in-memory store."
        # The SFAQ branch survives because `server.py` recovers the minted id through the
        # reverse link `questions.attributes.candidate_id`; terms have no such link.
        # So on BigQuery the payload key is always absent on a retry and this guard misses.
        #
        # The guard therefore is not the mechanism. The mechanism is that **the id is
        # derived from the candidate**, so a retry addresses the same row, and
        # `TermStore.add` upserts on `term_id` (MERGE). Two runs, one row — with no
        # dependence on anything having been persisted in between. Same shape as the SFAQ
        # `stable_id`. The payload key is kept because the in-memory store *does* persist
        # it, and it remains a useful audit trail there.
        if not payload.get("promoted_term_id"):
            # audience is pinned to internal, exactly as _promote_sfaq does: a promoted row
            # must not become PUBLIC (which `_audience_ok` grants to everyone, viewer or
            # not) merely because a proposal said so. Tier/sensitivity stay as proposed
            # (§3.3).
            scope = ScopeTags(
                candidate.proposed_scope.tier,
                ScopeAudience.INTERNAL,
                candidate.proposed_scope.sensitivity,
            )
            term_id = _term_id_for(candidate.candidate_id)
            term_sink.add(
                Term(
                    term_id=term_id,
                    term=payload.get("term", ""),
                    definition=payload.get("definition", ""),
                    basis=payload.get("basis"),
                    reading=payload.get("reading"),
                    scope=scope,
                    owner_unit=candidate.owner_unit,
                    attributes={"circles": candidate.proposed_circles},
                    status="approved",
                )
            )
            payload["promoted_term_id"] = term_id
    if candidate.kind == CandidateKind.SFAQ and knowledge_sink is not None:
        if not payload.get("promoted_question_id"):  # idempotent: never mint twice
            payload["promoted_question_id"] = _promote_sfaq(
                candidate, payload, knowledge_sink, approver_id
            )
    candidate.payload = payload
    candidate.status = CandidateStatus.APPROVED
    return candidate.status


# Only these payload keys survive into Question.attributes — an AI-authored payload is
# otherwise an unschema'd channel into a live table.
_SFAQ_ATTR_ALLOWLIST = ("source_refs", "support_count")


def _promote_sfaq(
    candidate: ExtractionCandidate,
    payload: dict,
    knowledge_sink: Any,
    approver_id: str | None,
) -> str:
    """Build a provisional Question (+ optional first Feedback) from an SFAQ candidate.

    Payload convention: ``question_text`` (required), ``judgment`` (optional first
    feedback body), ``fb_type`` (optional, default ``open``), ``provenance`` (optional,
    ``corporate_training``/``other`` only — ``individual`` is refused because this path
    cannot assert individual consent). ``perm_corporate`` is granted by the steward's
    approval (the caller must enforce the steward gate — see ServerContext).
    Fail-closed guards (extraction-derived content must not silently widen):
    audience is pinned to ``internal``; a dept/personal tier requires ``owner_unit``.
    Both served texts pass the deterministic scrub (INV-1); attributes are allowlisted.
    The first feedback's author is the organization (``author_id=None``) — the approver
    is recorded in attributes, not as the judgment's author.
    """
    from waca_loop.ingest.anonymize import scrub_text
    from waca_loop.knowledge.store import new_id
    from waca_loop.models import (
        FbType,
        Feedback,
        Provenance,
        Question,
        QuestionStatus,
        ScopeAudience,
        ScopeTier,
    )

    # Validate everything BEFORE the first insert (no orphan live rows on bad payloads).
    question_text = payload.get("question_text")
    if not question_text or not str(question_text).strip():
        raise ValueError(
            "sfaq candidate payload must carry non-empty 'question_text' "
            "(convention: question_text / judgment / fb_type / provenance)"
        )
    provenance = Provenance(payload.get("provenance", "corporate_training"))
    if provenance == Provenance.INDIVIDUAL:
        raise ValueError(
            "sfaq promotion cannot assert individual consent — file via "
            "add_sfaq_question (the self-filing path) instead"
        )
    fb_type = FbType(payload.get("fb_type", "open"))
    scope = candidate.proposed_scope
    if scope.audience != ScopeAudience.INTERNAL:
        raise ValueError("sfaq promotion pins audience=internal (no public questions)")
    if scope.tier in (ScopeTier.DEPT, ScopeTier.PERSONAL) and not candidate.owner_unit:
        raise ValueError(f"scope tier '{scope.tier.value}' requires owner_unit")

    attributes = {k: payload[k] for k in _SFAQ_ATTR_ALLOWLIST if k in payload}
    attributes["candidate_id"] = candidate.candidate_id
    if approver_id:
        attributes["approved_by"] = approver_id
    if candidate.proposed_circles:
        attributes["circles"] = list(candidate.proposed_circles)
    q = Question(
        question_id=new_id("q"),
        question_text=scrub_text(str(question_text)).text,  # INV-1: deterministic scrub
        scope=scope,
        provenance=provenance,
        owner_unit=candidate.owner_unit,
        attributes=attributes,
        perm_corporate=True,
        status=QuestionStatus.PROVISIONAL,
        source=candidate.source,
    )
    knowledge_sink.add_question(q)
    if payload.get("judgment"):
        knowledge_sink.add_feedback(
            Feedback(
                feedback_id=new_id("fb"),
                question_id=q.question_id,
                fb_type=fb_type,
                body=scrub_text(str(payload["judgment"])).text,
                author_id=None,  # the org, not the approver, owns the judgment
            )
        )
    return q.question_id
