"""PolicyWorkbench: log-aware policy coaching sessions.

This module reads summary-only usage signals, policy guides, and adoption rows to start a
conversation that produces a concrete artifact. It does not write the ledger and never sees
raw prompts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from waca_loop.ledger.usage_log import UsageSummary
from waca_loop.models import Viewer
from waca_loop.policy.adoption import AdoptionRow
from waca_loop.policy.knowledge import PolicyFloor
from waca_loop.policy.protocols import AdoptionMapStore, PolicyGuideStore

_ARTIFACT_TYPES = frozenset({
    "program",
    "document",
    "manual",
    "checklist",
    "template",
    "unspecified",
})
_LOWER_BOUND_TERMS = (
    "個人情報",
    "氏名",
    "メール",
    "会員番号",
    "決済",
    "ログイン",
    "認証情報",
    "名簿全件",
    "正答一覧",
    "採点基準",
    "最終判断",
)


class UsageSummaryReader(Protocol):
    """Summary-only ledger reader used by PolicyWorkbench."""

    def recent_summaries_for_actor(
        self,
        viewer: Viewer,
        actor_id: str,
        *,
        limit: int = 20,
        not_before: datetime | None = None,
    ) -> list[UsageSummary]: ...


@dataclass(frozen=True, kw_only=True)
class WorkbenchRequest:
    """Start request for a policy workbench session."""

    viewer: Viewer
    goal_hint_summary: str | None = None
    artifact_type: str | None = None
    max_logs: int = 20
    # Bind floor (SPEC_export_my_usage §4.1): only summaries at/after this moment are used.
    logs_not_before: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class RecommendedArtifact:
    """The concrete thing the session should build next."""

    type: str
    title: str

    def as_dict(self) -> dict:
        return {"type": self.type, "title": self.title}


@dataclass(frozen=True, kw_only=True)
class WorkbenchResponse:
    """Structured response for user-facing conversation and machine-readable evidence."""

    session_id: str
    current_state: str
    desired_state: str
    recommended_artifact: RecommendedArtifact
    policy_explanation: str
    first_question: str
    source_policy_row_ids: list[str] = field(default_factory=list)
    source_adoption_row_ids: list[str] = field(default_factory=list)
    source_log_ids: list[str] = field(default_factory=list)
    lower_bound_note: str | None = None
    diagnostic: str = "ok"
    not_for_evaluation: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict:
        out = {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "current_state": self.current_state,
            "desired_state": self.desired_state,
            "recommended_artifact": self.recommended_artifact.as_dict(),
            "policy_explanation": self.policy_explanation,
            "first_question": self.first_question,
            "source_policy_row_ids": list(self.source_policy_row_ids),
            "source_adoption_row_ids": list(self.source_adoption_row_ids),
            "source_log_ids": list(self.source_log_ids),
            "diagnostic": self.diagnostic,
            "not_for_evaluation": self.not_for_evaluation,
        }
        if self.lower_bound_note:
            out["lower_bound_note"] = self.lower_bound_note
        return out


class PolicyWorkbench:
    """Start a log-aware coaching session that leads to a concrete artifact."""

    def __init__(
        self,
        *,
        policy_guides: PolicyGuideStore | None,
        adoption_map: AdoptionMapStore | None,
        usage_reader: UsageSummaryReader | None,
    ) -> None:
        self._policy_guides = policy_guides
        self._adoption_map = adoption_map
        self._usage_reader = usage_reader

    def start(self, request: WorkbenchRequest) -> WorkbenchResponse:
        max_logs = min(max(int(request.max_logs), 0), 20)
        summaries = self._usage_summaries(request.viewer, max_logs, request.logs_not_before)
        rows = self._adoption_rows(request.viewer)
        floors = self._floors(request.viewer)
        selected = _select_row(rows, request.goal_hint_summary, summaries)
        artifact = _artifact_for(request.artifact_type, selected, request.goal_hint_summary)
        source_policy_ids = _source_policy_ids(
            selected, floors, request.goal_hint_summary, summaries
        )
        source_log_ids = [s.log_id for s in summaries[:5]]
        lower_bound_note = _lower_bound_note(
            selected, floors, request.goal_hint_summary, summaries
        )

        return WorkbenchResponse(
            session_id=f"workbench:{uuid.uuid4()}",
            current_state=_current_state(summaries),
            desired_state=_desired_state(artifact, selected),
            recommended_artifact=artifact,
            policy_explanation=_policy_explanation(selected, artifact, lower_bound_note),
            first_question=_first_question(artifact),
            source_policy_row_ids=source_policy_ids,
            source_adoption_row_ids=[selected.row_id] if selected else [],
            source_log_ids=source_log_ids,
            lower_bound_note=lower_bound_note,
            diagnostic=_diagnostic(summaries, rows, self._policy_guides, self._adoption_map),
        )

    def _usage_summaries(
        self, viewer: Viewer, limit: int, not_before: datetime | None = None
    ) -> list[UsageSummary]:
        if self._usage_reader is None or viewer.person_id is None or limit <= 0:
            return []
        got = self._usage_reader.recent_summaries_for_actor(
            viewer, viewer.person_id, limit=limit, not_before=not_before
        )
        if not_before is None:
            return got
        # §4.1: second gate on the bind floor, in-process.
        return [s for s in got if s.occurred_at >= not_before]

    def _adoption_rows(self, viewer: Viewer) -> list[AdoptionRow]:
        if self._adoption_map is None:
            return []
        return self._adoption_map.rows_for_person(viewer)

    def _floors(self, viewer: Viewer) -> list[PolicyFloor]:
        if self._policy_guides is None:
            return []
        return self._policy_guides.floors_for(viewer)


def _diagnostic(
    summaries: list[UsageSummary],
    rows: list[AdoptionRow],
    policy_guides: PolicyGuideStore | None,
    adoption_map: AdoptionMapStore | None,
) -> str:
    if policy_guides is None or adoption_map is None:
        return "partial"
    if not summaries and not rows:
        return "sparse"
    return "ok"


def _select_row(
    rows: list[AdoptionRow],
    goal_hint_summary: str | None,
    summaries: list[UsageSummary],
) -> AdoptionRow | None:
    if not rows:
        return None
    needle = " ".join([goal_hint_summary or "", *[s.summary for s in summaries]]).lower()
    return max(rows, key=lambda row: _row_score(row, needle))


def _row_score(row: AdoptionRow, needle: str) -> tuple:
    text = " ".join([
        row.business,
        row.current_usage,
        row.safe_practice,
        row.issue_summary or "",
        row.next_step or "",
        row.manager_guidance or "",
        " ".join(row.person_intent.values()),
    ]).lower()
    level_gap = (row.target_level or row.current_level) - row.current_level
    return (
        any(part and part in needle for part in text.split()[:8]),
        bool(row.person_intent),
        level_gap,
        bool(row.next_step),
        row.row_id,
    )


def _artifact_for(
    artifact_type: str | None, row: AdoptionRow | None, goal_hint_summary: str | None
) -> RecommendedArtifact:
    kind = (artifact_type or "").strip().lower() or _infer_artifact_type(row, goal_hint_summary)
    if kind not in _ARTIFACT_TYPES:
        kind = "unspecified"
    business = row.business if row is not None else _topic_from_goal(goal_hint_summary)
    title_by_kind = {
        "program": f"{business}を安全に処理するスクリプト雛形",
        "document": f"{business}の資料たたき台",
        "manual": f"{business}の運用マニュアル",
        "checklist": f"{business}の一次対応チェックリスト",
        "template": f"{business}の再利用テンプレート",
        "unspecified": f"{business}の再利用できる成果物",
    }
    return RecommendedArtifact(type=kind, title=title_by_kind[kind])


def _infer_artifact_type(row: AdoptionRow | None, goal_hint_summary: str | None) -> str:
    text = " ".join([
        goal_hint_summary or "",
        row.business if row else "",
        row.next_step if row and row.next_step else "",
    ])
    if any(word in text for word in ("コード", "スクリプト", "プログラム", "自動化")):
        return "program"
    if any(word in text for word in ("マニュアル", "手順")):
        return "manual"
    if any(word in text for word in ("チェック", "確認", "一次対応")):
        return "checklist"
    if "テンプレ" in text:
        return "template"
    if any(word in text for word in ("資料", "スライド", "文書")):
        return "document"
    return "checklist" if row else "unspecified"


def _topic_from_goal(goal_hint_summary: str | None) -> str:
    if not goal_hint_summary:
        return "最近のAI活用"
    return goal_hint_summary.strip()[:32]


def _current_state(summaries: list[UsageSummary]) -> str:
    if not summaries:
        return "直近ログだけでは、まだAI活用の傾向を十分に読めません。"
    snippets = [s.summary for s in summaries[:3] if s.summary]
    return "最近のログでは、" + "、".join(snippets) + "にAIを使っています。"


def _desired_state(artifact: RecommendedArtifact, row: AdoptionRow | None) -> str:
    if row and row.target_level is not None and row.target_level > row.current_level:
        return f"単発相談で終わらせず、{artifact.title}として再利用できる状態にします。"
    return f"最近の相談を、{artifact.title}として繰り返し使える形にします。"


def _policy_explanation(
    row: AdoptionRow | None, artifact: RecommendedArtifact, lower_bound_note: str | None
) -> str:
    if row is None:
        return f"{artifact.title}を作る前提で、使える情報だけを小さく整理して進めます。"
    if lower_bound_note:
        return lower_bound_note
    if row.safe_practice:
        return f"{row.business}は、{row.safe_practice}という形なら進められます。"
    if row.next_step:
        return f"{row.business}は、{row.next_step}から始めるのが安全です。"
    return f"{row.business}は、下書きや手順整理として使い、人が確認する前提で進められます。"


def _first_question(artifact: RecommendedArtifact) -> str:
    if artifact.type == "program":
        return "最初に自動化したい入力と出力を、1つずつ教えてください。"
    if artifact.type == "manual":
        return "このマニュアルの読者は、初めて作業する人ですか、それとも経験者ですか？"
    if artifact.type == "document":
        return "この資料の読み手と、読み終えた後に取ってほしい行動を教えてください。"
    if artifact.type == "template":
        return "毎回変わる項目を3つ挙げると、何になりますか？"
    return "最初の分類を3つに分けるなら、何と何と何になりますか？"


def _source_policy_ids(
    row: AdoptionRow | None,
    floors: list[PolicyFloor],
    goal_hint_summary: str | None,
    summaries: list[UsageSummary],
) -> list[str]:
    ids: list[str] = []
    for summary in summaries:
        ids.extend(summary.policy_row_ids)
    if _touches_lower_bound(row, goal_hint_summary, summaries):
        ids.extend(f.row_id for f in floors)
    return list(dict.fromkeys(ids))


def _lower_bound_note(
    row: AdoptionRow | None,
    floors: list[PolicyFloor],
    goal_hint_summary: str | None,
    summaries: list[UsageSummary],
) -> str | None:
    if not floors or not _touches_lower_bound(row, goal_hint_summary, summaries):
        return None
    topic = row.business if row else _topic_from_goal(goal_hint_summary)
    return (
        f"{topic}は、実データそのものではなく、架空例または要約で型を作ってから"
        "人が実環境で確認する形にします。"
    )


def _touches_lower_bound(
    row: AdoptionRow | None,
    goal_hint_summary: str | None,
    summaries: list[UsageSummary],
) -> bool:
    text = " ".join([
        goal_hint_summary or "",
        row.business if row else "",
        row.current_usage if row else "",
        (row.issue_summary if row else "") or "",
        *[s.summary for s in summaries],
        *[kind for s in summaries for kind in s.data_kinds],
    ])
    return any(term in text for term in _LOWER_BOUND_TERMS)


__all__ = [
    "PolicyWorkbench",
    "RecommendedArtifact",
    "UsageSummaryReader",
    "WorkbenchRequest",
    "WorkbenchResponse",
]
