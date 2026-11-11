"""Personal AI-adoption coach for Lite (SPEC_lite_policy_knowledge_store_v1 §5).

The coach is deliberately rule-based: it combines Docs-derived policy context with
Sheets-derived adoption rows and returns explainable next steps. It never receives
or stores raw prompts; callers may pass only a short ``task_hint_summary``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from waca_loop.models import Viewer
from waca_loop.policy.adoption import AdoptionRow
from waca_loop.policy.knowledge import PolicyFloor, SafeChannel
from waca_loop.policy.protocols import AdoptionMapStore, PolicyGuideStore
from waca_loop.retrieval.acl import ORG_UNIT


@dataclass(frozen=True, kw_only=True)
class CoachRequest:
    """Input to ``PersonalCoach`` (SPEC_lite_policy_knowledge_store_v1 §5.2)."""

    viewer: Viewer
    task_hint_summary: str | None = None
    mode: str = "on_demand"  # on_demand | scheduled
    max_items: int = 3


@dataclass(frozen=True, kw_only=True)
class CoachRecommendation:
    """One safe next step for a person."""

    recommendation_id: str
    target: str
    title: str
    reason: str
    next_step: str
    suggested_level: int | None
    safety_path: str | None = None
    required_controls: list[str] = field(default_factory=list)
    detour: str | None = None
    source_policy_row_ids: list[str] = field(default_factory=list)
    source_adoption_row_ids: list[str] = field(default_factory=list)
    source_question_ids: list[str] = field(default_factory=list)
    source_term_ids: list[str] = field(default_factory=list)
    output_channel: str = "on_demand"
    status: str = "proposed"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict:
        return {
            "recommendation_id": self.recommendation_id,
            "created_at": self.created_at.isoformat(),
            "target": self.target,
            "title": self.title,
            "reason": self.reason,
            "next_step": self.next_step,
            "suggested_level": self.suggested_level,
            "safety_path": self.safety_path,
            "required_controls": list(self.required_controls),
            "detour": self.detour,
            "source_policy_row_ids": list(self.source_policy_row_ids),
            "source_adoption_row_ids": list(self.source_adoption_row_ids),
            "source_question_ids": list(self.source_question_ids),
            "source_term_ids": list(self.source_term_ids),
            "output_channel": self.output_channel,
            "status": self.status,
        }


@dataclass(frozen=True, kw_only=True)
class CoachResponse:
    """Response wrapper that keeps the anti-evaluation boundary visible."""

    target_person_id: str | None
    mode: str
    recommendations: list[CoachRecommendation]
    not_for_evaluation: bool = True

    def as_dict(self) -> dict:
        return {
            "target_person_id": self.target_person_id,
            "mode": self.mode,
            "not_for_evaluation": self.not_for_evaluation,
            "recommendations": [r.as_dict() for r in self.recommendations],
        }


class UsageSummaryReader(Protocol):
    """Optional summary-only usage reader. No raw prompt surface by design."""

    def summaries_for_actor(self, actor_id: str, *, limit: int = 5) -> list[str]: ...


class PersonalCoach:
    """Return safe, explainable next steps for the requesting person."""

    def __init__(
        self,
        *,
        policy_guides: PolicyGuideStore,
        adoption_map: AdoptionMapStore,
        usage_reader: UsageSummaryReader | None = None,
    ) -> None:
        self._policy_guides = policy_guides
        self._adoption_map = adoption_map
        self._usage_reader = usage_reader

    def recommend(self, request: CoachRequest) -> CoachResponse:
        rows = self._adoption_map.rows_for_person(request.viewer)
        floors = self._policy_guides.floors_for(request.viewer)
        channels = self._policy_guides.safe_channels_for(request.viewer)
        summaries = self._usage_summaries(request.viewer)

        ranked = sorted(
            (r for r in rows if _recommendable(r, channels)),
            key=lambda r: _priority(r, request.task_hint_summary, summaries),
            reverse=True,
        )
        recommendations = [
            self._recommendation_for(row, floors, channels, request)
            for row in ranked[: max(0, request.max_items)]
        ]
        return CoachResponse(
            target_person_id=request.viewer.person_id,
            mode=request.mode,
            recommendations=recommendations,
        )

    def _usage_summaries(self, viewer: Viewer) -> list[str]:
        if self._usage_reader is None or viewer.person_id is None:
            return []
        return self._usage_reader.summaries_for_actor(viewer.person_id, limit=5)

    def _recommendation_for(
        self,
        row: AdoptionRow,
        floors: list[PolicyFloor],
        channels: list[SafeChannel],
        request: CoachRequest,
    ) -> CoachRecommendation:
        target = row.actor_scope or row.scope_unit
        suggested = row.target_level if row.target_level is not None else row.current_level
        controls = _controls_for(row, channels)
        safety_path = row.safe_practice or _channel_path(controls)
        detour = row.next_step if row.current_level == 0 else None
        if row.current_level == 0 and not detour:
            detour = safety_path
        return CoachRecommendation(
            recommendation_id=f"coach:{row.row_id}",
            target=target,
            title=f"次に試す: {row.business}",
            reason=_reason(row),
            next_step=row.next_step or _fallback_next_step(row),
            suggested_level=suggested,
            safety_path=safety_path,
            required_controls=controls,
            detour=detour,
            source_policy_row_ids=_floor_ids_for(row, floors),
            source_adoption_row_ids=[row.row_id],
            output_channel=request.mode,
        )


def _recommendable(row: AdoptionRow, channels: list[SafeChannel]) -> bool:
    if _has_suspended_channel(row, channels):
        return False
    if row.current_level == 0:
        return bool(row.safe_practice or row.next_step or _controls_for(row, channels))
    if row.target_level is not None and row.target_level > row.current_level:
        return True
    return bool(row.person_intent or row.next_step or row.manager_guidance)


def _priority(row: AdoptionRow, task_hint_summary: str | None, summaries: list[str]) -> tuple:
    intent_text = " ".join(row.person_intent.values())
    text = " ".join([
        row.business,
        intent_text,
        row.issue_summary or "",
        row.next_step or "",
        row.manager_guidance or "",
    ])
    task_match = bool(task_hint_summary and task_hint_summary in text)
    usage_match = any(row.business in summary for summary in summaries)
    level_gap = (row.target_level or row.current_level) - row.current_level
    return (
        bool(row.person_intent),
        level_gap,
        task_match,
        usage_match,
        bool(row.manager_guidance),
        bool(row.next_step),
        row.row_id,
    )


def _reason(row: AdoptionRow) -> str:
    if row.person_intent:
        wanted = row.person_intent.get("try_next") or row.person_intent.get("wanted_work")
        if wanted:
            return f"本人が試したい業務として挙げているため: {wanted}"
    if row.target_level is not None and row.target_level > row.current_level:
        return f"現状レベル{row.current_level}から目標レベル{row.target_level}へ進める候補です"
    if row.manager_guidance:
        return f"上長指導に沿った支援候補です: {row.manager_guidance}"
    if row.issue_summary:
        return f"課題として挙がっているため: {row.issue_summary}"
    return "安全に試せる次の一歩として候補化しました"


def _fallback_next_step(row: AdoptionRow) -> str:
    if row.person_intent.get("try_next"):
        return row.person_intent["try_next"]
    if row.safe_practice:
        return row.safe_practice
    return "小さな業務単位に分け、人が確認する前提で試す"


def _controls_for(row: AdoptionRow, channels: list[SafeChannel]) -> list[str]:
    controls: list[str] = []
    for channel in channels:
        if channel.scope_unit not in {row.scope_unit, ORG_UNIT}:
            continue
        if channel.status not in {"trial", "approved"}:
            continue
        controls.extend(channel.required_controls)
    return list(dict.fromkeys(controls))


def _has_suspended_channel(row: AdoptionRow, channels: list[SafeChannel]) -> bool:
    return any(
        channel.status == "suspended" and channel.scope_unit in {row.scope_unit, ORG_UNIT}
        for channel in channels
    )


def _channel_path(controls: list[str]) -> str | None:
    if not controls:
        return None
    return "安全チャネルで必要な統制を満たして実行する"


def _floor_ids_for(row: AdoptionRow, floors: list[PolicyFloor]) -> list[str]:
    text = " ".join([
        row.business,
        row.current_usage,
        row.safe_practice,
        row.issue_summary or "",
        row.next_step or "",
    ]).lower()
    matched: list[str] = []
    for floor in floors:
        triggers = [t.lower() for t in floor.trigger_kinds if t.strip()]
        if triggers and any(t in text for t in triggers):
            matched.append(floor.row_id)
        elif not triggers and row.current_level == 0:
            matched.append(floor.row_id)
    return matched
