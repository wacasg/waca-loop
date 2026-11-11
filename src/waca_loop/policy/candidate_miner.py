"""Candidate miner for Lite Policy / Knowledge Store.

This module turns summarized signals into TERMS/SFAQ candidates and separate
policy-revision candidates. It never approves candidates and never creates GitHub
issues; it only returns structured proposals for a steward/admin workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from waca_loop.ingest.anonymize import scrub_text
from waca_loop.models import (
    CandidateKind,
    ExtractionCandidate,
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Viewer,
)
from waca_loop.policy.protocols import KnowledgeCandidateStore
from waca_loop.policy.safe_ref import safe_source_ref


@dataclass(frozen=True, kw_only=True)
class CandidateMiningRequest:
    """Summary-only mining request (SPEC_lite_policy_knowledge_store_v1 §6.2)."""

    viewer: Viewer
    source_kind: str
    source_ref: str
    summary: str
    policy_row_ids: list[str] = field(default_factory=list)
    owner_unit: str | None = None


@dataclass(frozen=True, kw_only=True)
class PolicyRevisionCandidate:
    """Policy revision proposal kept separate from TERMS/SFAQ candidates."""

    candidate_id: str
    source_kind: str
    source_ref: str
    summary: str
    reason: str
    policy_row_ids: list[str] = field(default_factory=list)
    owner_unit: str | None = None
    status: str = "proposed"

    def as_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "summary_recorded": bool(self.summary),
            "reason": self.reason,
            "policy_row_ids": list(self.policy_row_ids),
            "owner_unit": self.owner_unit,
            "status": self.status,
        }


@dataclass(frozen=True, kw_only=True)
class CandidateMiningResponse:
    """Mining output. TERMS/SFAQ ids point at ``KnowledgeCandidateStore`` rows."""

    candidate_ids: list[str]
    term_candidate_ids: list[str] = field(default_factory=list)
    sfaq_candidate_ids: list[str] = field(default_factory=list)
    policy_candidates: list[PolicyRevisionCandidate] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "candidate_ids": list(self.candidate_ids),
            "term_candidate_ids": list(self.term_candidate_ids),
            "sfaq_candidate_ids": list(self.sfaq_candidate_ids),
            "policy_candidates": [c.as_dict() for c in self.policy_candidates],
        }


class CandidateMiner:
    """Create steward-review candidates from summarized signals."""

    def __init__(self, *, candidate_store: KnowledgeCandidateStore) -> None:
        self._candidate_store = candidate_store

    def mine(self, request: CandidateMiningRequest) -> CandidateMiningResponse:
        if not request.viewer.is_org_member or not request.summary.strip():
            return CandidateMiningResponse(candidate_ids=[])

        summary = _safe_summary(request.summary)
        source_ref = safe_source_ref(request.source_kind, request.source_ref)
        safe_request = CandidateMiningRequest(
            viewer=request.viewer,
            source_kind=request.source_kind,
            source_ref=source_ref,
            summary=summary,
            policy_row_ids=list(request.policy_row_ids),
            owner_unit=request.owner_unit,
        )
        candidate_ids: list[str] = []
        term_candidate_ids: list[str] = []
        sfaq_candidate_ids: list[str] = []
        for candidate in _knowledge_candidates(safe_request, summary):
            candidate_id = self._candidate_store.add(candidate)
            candidate_ids.append(candidate_id)
            if candidate.kind is CandidateKind.TERM:
                term_candidate_ids.append(candidate_id)
            elif candidate.kind is CandidateKind.SFAQ:
                sfaq_candidate_ids.append(candidate_id)

        policy_candidates = _policy_candidates(safe_request, summary)
        return CandidateMiningResponse(
            candidate_ids=candidate_ids,
            term_candidate_ids=term_candidate_ids,
            sfaq_candidate_ids=sfaq_candidate_ids,
            policy_candidates=policy_candidates,
        )


def _knowledge_candidates(
    request: CandidateMiningRequest, summary: str
) -> list[ExtractionCandidate]:
    out: list[ExtractionCandidate] = []
    if request.source_kind in {"term_gap", "search_failure"} or "用語" in summary:
        label = _label_from_summary(summary)
        out.append(
            ExtractionCandidate(
                kind=CandidateKind.TERM,
                payload={
                    "term": label,
                    "definition": "要確認: 利用記録または改善要望から不足候補として検出",
                    "basis": f"source_ref:{request.source_ref}",
                },
                proposed_scope=_scope(),
                source=request.source_kind,
                source_ref=request.source_ref,
                owner_unit=request.owner_unit,
            )
        )
    if request.source_kind in {
        "sfaq_gap",
        "search_failure",
        "red_team",
        "feedback",
        "improvement_request",
    } or "判断" in summary:
        label = _label_from_summary(summary)
        out.append(
            ExtractionCandidate(
                kind=CandidateKind.SFAQ,
                payload={
                    "question_text": _question_from_label(label),
                    "judgment": "要裁定",
                    "fb_type": "open",
                    "source_refs": [request.source_ref],
                    "support_count": 1,
                    "basis": f"source_ref:{request.source_ref}",
                },
                proposed_scope=_scope(),
                source=request.source_kind,
                source_ref=request.source_ref,
                owner_unit=request.owner_unit,
            )
        )
    return out


def _policy_candidates(
    request: CandidateMiningRequest, summary: str
) -> list[PolicyRevisionCandidate]:
    triggers = (
        "ポリシー",
        "方針",
        "矛盾",
        "安全チャネル不明",
        "不明",
        "迂回",
        "red team",
        "レッドチーム",
    )
    if request.source_kind != "red_team" and not any(t in summary for t in triggers):
        return []
    return [
        PolicyRevisionCandidate(
            candidate_id=f"policy:{request.source_kind}:{request.source_ref}",
            source_kind=request.source_kind,
            source_ref=request.source_ref,
            summary=summary,
            reason="Docs方針またはSheets活用行の更新候補として確認が必要",
            policy_row_ids=list(request.policy_row_ids),
            owner_unit=request.owner_unit,
        )
    ]


def _scope() -> ScopeTags:
    return ScopeTags(ScopeTier.DEPT, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL)


def _safe_summary(summary: str) -> str:
    return scrub_text(summary).text.strip()[:500]


def _label_from_summary(summary: str) -> str:
    if "「" in summary and "」" in summary:
        return summary.split("「", 1)[1].split("」", 1)[0][:40]
    if ":" in summary:
        return summary.split(":", 1)[0].strip()[:40]
    return "要確認候補"


def _question_from_label(label: str) -> str:
    return f"{label} — この種のケースではどう判断するか？"
