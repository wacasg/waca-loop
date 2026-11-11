"""Lite Sheets-backed policy knowledge stores.

This module keeps Google Sheets details at the backend edge. The class can be
fed already-read tab rows in tests, and later a small reader can supply the same
shape from the live Sheets API. It stores only normalized summaries and stable
ids; raw prompts/transcripts never enter this layer.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from waca_loop.models import Viewer
from waca_loop.org import get_org_config
from waca_loop.policy.adoption import AdoptionRow
from waca_loop.policy.knowledge import PolicyFloor, PolicyGuide, RefreshResult, SafeChannel
from waca_loop.policy.memory import InMemoryAdoptionMapStore, InMemoryPolicyGuideStore
from waca_loop.policy.safe_ref import safe_source_ref
from waca_loop.retrieval.acl import ORG_UNIT

logger = logging.getLogger(__name__)

CONTROL_TABS = frozenset({
    "ルール",
    "policy_sources",
    "policy_guides",
    "policy_floors",
    "safe_channels",
    "policy_checks",
    "policy_candidates",
})

UNFILLED = frozenset({"", "（記入）", "(記入)", "-", "—"})
SUSPENDED_CHANNEL_STATUSES = frozenset({"suspended"})
ADOPTION_SOURCE_TYPES = frozenset({
    "adoption",
    "adoption_map",
    "department_policy",
    "person_policy",
    "policy_tab",
})
GUIDE_SOURCE_TYPES = frozenset({"guide", "policy_guide", "doc", "docs", "document"})

SHARE_SCOPE_ALIASES = {
    "person": "person",
    "private": "person",
    "本人のみ": "person",
    "非公開": "person",
    "個人": "person",
    "個人のみ": "person",
    "unit": "unit",
    "部署": "unit",
    "課": "unit",
    "部": "unit",
    "部署内": "unit",
    "org": "org",
    "全社": "org",
    "組織": "org",
    "manager": "manager",
    "上長": "manager",
    "管理者": "admin",
    "admin": "admin",
}

ADOPTION_COLUMNS = {
    "no": ("No.", "No", "row_id"),
    "business": ("業務", "line", "business"),
    "current_level": ("現状レベル(0-2)", "現状レベル（0-2）", "現状レベル", "current_level"),
    "current_usage": (
        "現状の使い方・安全な進め方",
        "現状の使い方",
        "safe_practice",
        "current_usage",
    ),
    "target_level": ("目標レベル(0-2)", "目標レベル（0-2）", "目標レベル", "target_level"),
    "next_step": ("目標（次の一歩・期限）", "目標", "試したいこと", "next_step"),
    "issue_summary": ("課題", "不安・懸念", "issue_summary"),
    "owner_label": ("担当", "owner_label"),
    "manager_guidance": (
        "上長指導内容（部署責任者・管理者記入）",
        "上長指導内容",
        "manager_guidance",
    ),
    "updated_at": ("更新日", "updated_at"),
    "share_scope": ("LL共有範囲", "share_scope"),
    "wanted_work": ("本人が使いたい業務", "wanted_work"),
    "try_next": ("試したいこと", "try_next"),
    "concern": ("不安・懸念", "concern"),
    "improvement_request": ("改善希望", "improvement_request"),
    "actor_scope": ("actor_scope", "person_id", "person"),
    "scope_unit": ("scope_unit", "部署コード", "部署名"),
    "status": ("status", "状態"),
}


@dataclass(frozen=True, kw_only=True)
class PolicySource:
    """A row from ``policy_sources`` controlling refresh cadence."""

    source_id: str
    source_type: str
    title: str
    url: str = ""
    owner_unit: str = ORG_UNIT
    refresh_frequency: str | None = None
    next_refresh_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    source_version: str | None = None
    checksum: str | None = None
    status: str = "active"
    tab_name: str | None = None


class SheetPolicyReader(Protocol):
    """Live reader boundary for future Google Sheets API integration."""

    def read_source(self, source: PolicySource) -> Mapping[str, Sequence[Mapping[str, object]]]: ...


class LiteSheetsPolicyStore:
    """PolicyGuideStore + AdoptionMapStore over normalized Sheets rows."""

    def __init__(
        self,
        tabs: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
        *,
        sources: Sequence[PolicySource] | None = None,
        reader: SheetPolicyReader | None = None,
        unit_map: Mapping[str, str] | None = None,
        workspace_domains: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> None:
        self._reader = reader
        self._unit_map = {str(k): str(v) for k, v in (unit_map or {}).items()}
        self._workspace_domains = frozenset(
            d.strip().lower()
            for d in (workspace_domains or get_org_config().workspace_domains)
            if d.strip()
        )
        self._sources: dict[str, PolicySource] = {s.source_id: s for s in sources or []}
        self._last_results: dict[str, RefreshResult] = {}
        self._failure_counts: dict[str, int] = {}
        self._apply_tabs(tabs or {}, now=now or datetime.now(UTC))

    @classmethod
    def from_grids(
        cls,
        grids: Mapping[str, Sequence[Sequence[object]]],
        *,
        sources: Sequence[PolicySource] | None = None,
        reader: SheetPolicyReader | None = None,
        unit_map: Mapping[str, str] | None = None,
        workspace_domains: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> LiteSheetsPolicyStore:
        return cls(
            {tab: rows_from_grid(grid) for tab, grid in grids.items()},
            sources=sources,
            reader=reader,
            unit_map=unit_map,
            workspace_domains=workspace_domains,
            now=now,
        )

    @classmethod
    def from_json_file(
        cls,
        path: str | Path,
        *,
        workspace_domains: Sequence[str] | None = None,
    ) -> LiteSheetsPolicyStore:
        """Load a Lite snapshot exported from Sheets.

        Accepted shapes:
        - ``{"tabs": {"tab": [{"col": "value"}]}}``
        - ``{"grids": {"tab": [["header"], ["value"]]}}``
        - ``{"tab": [{"col": "value"}]}``
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Lite policy store JSON must be an object")
        if isinstance(data.get("grids"), dict):
            return cls.from_grids(data["grids"], workspace_domains=workspace_domains)
        tabs = data.get("tabs", data)
        if not isinstance(tabs, dict):
            raise ValueError("Lite policy store JSON tabs must be an object")
        return cls(tabs, workspace_domains=workspace_domains)

    def guides_for(self, viewer: Viewer) -> list[PolicyGuide]:
        return self._guides.guides_for(viewer)

    def floors_for(self, viewer: Viewer) -> list[PolicyFloor]:
        return self._guides.floors_for(viewer)

    def safe_channels_for(self, viewer: Viewer) -> list[SafeChannel]:
        return self._guides.safe_channels_for(viewer)

    def rows_for_person(self, viewer: Viewer) -> list[AdoptionRow]:
        return self._adoption.rows_for_person(viewer)

    def rows_for_unit(self, viewer: Viewer, scope_unit: str | None = None) -> list[AdoptionRow]:
        return self._adoption.rows_for_unit(viewer, scope_unit)

    def refresh_due_sources(self, now: datetime) -> list[str]:
        return [
            s.source_id for s in self._sources.values()
            if s.status in {"active", "trial", "approved"}
            and _next_refresh_at(s) is not None
            and _next_refresh_at(s) <= now
        ]

    def refresh_source(self, source_id: str) -> RefreshResult:
        source = self._sources.get(source_id)
        refreshed_at = datetime.now(UTC)
        if source is None:
            return self._remember(RefreshResult(
                source_id=source_id,
                changed=False,
                success=False,
                message="policy source is not registered",
                refreshed_at=refreshed_at,
            ))
        if self._reader is None:
            return self._remember(RefreshResult(
                source_id=source_id,
                changed=False,
                success=False,
                message="policy source reader is not configured",
                refreshed_at=refreshed_at,
                source_version=source.source_version,
                checksum=source.checksum,
            ))
        old_checksum = self._checksum
        try:
            tabs = self._reader.read_source(source)
            if not tabs:
                raise EmptySnapshotError("empty policy snapshot")
            self._apply_tabs(tabs, now=refreshed_at)
        except EmptySnapshotError:
            logger.warning("waca-loop: Lite policy refresh returned an empty snapshot")
            return self._refresh_failure(
                source, source_id, refreshed_at, old_checksum, kind="empty_snapshot"
            )
        except Exception as exc:  # pragma: no cover - exact reader failure type is external.
            logger.exception("waca-loop: Lite policy refresh failed: %s", type(exc).__name__)
            kind = _error_kind(exc)
            return self._refresh_failure(source, source_id, refreshed_at, old_checksum, kind=kind)
        self._failure_counts[source_id] = 0
        self._sources[source_id] = _mark_refreshed(source, refreshed_at, self._checksum)
        source = self._sources[source_id]
        message = "refreshed policy source"
        if source.next_refresh_at is not None:
            message += f"; next_refresh_at={source.next_refresh_at.isoformat()}"
        return self._remember(RefreshResult(
            source_id=source_id,
            changed=self._checksum != old_checksum,
            success=True,
            message=message,
            refreshed_at=refreshed_at,
            source_version=source.source_version,
            checksum=self._checksum,
        ))

    def _refresh_failure(
        self,
        source: PolicySource,
        source_id: str,
        refreshed_at: datetime,
        old_checksum: str,
        *,
        kind: str,
    ) -> RefreshResult:
        self._failure_counts[source_id] = self._failure_counts.get(source_id, 0) + 1
        stale = "; stale_warning=true" if self._failure_counts[source_id] >= 3 else ""
        return self._remember(RefreshResult(
            source_id=source_id,
            changed=False,
            success=False,
            message=f"refresh failed ({kind}); using last valid snapshot{stale}",
            refreshed_at=refreshed_at,
            source_version=source.source_version,
            checksum=old_checksum,
        ))

    def last_refresh_result(self, source_id: str) -> RefreshResult | None:
        return self._last_results.get(source_id)

    def _remember(self, result: RefreshResult) -> RefreshResult:
        self._last_results[result.source_id] = result
        return result

    def _apply_tabs(
        self,
        tabs: Mapping[str, Sequence[Mapping[str, object]]],
        *,
        now: datetime,
    ) -> None:
        _ = now  # reserved for a future last-refreshed snapshot without changing checksums
        sources = dict(self._sources)
        sources.update(_parse_sources(tabs.get("policy_sources", [])))
        allowed_tabs = _adoption_tab_allowlist(sources)
        guides = _parse_guides(tabs.get("policy_guides", []))
        floors = _parse_floors(tabs.get("policy_floors", []))
        safe_channels = _parse_safe_channels(tabs.get("safe_channels", []))
        if tabs and not (guides or floors or safe_channels or allowed_tabs):
            raise EmptySnapshotError("policy snapshot has no servable policy tabs")
        adoption_rows = [
            row
            for tab_name, rows in tabs.items()
            if _adoption_tab_allowed(tab_name, allowed_tabs)
            for row in _parse_adoption_rows(
                tab_name, rows, self._unit_map, self._workspace_domains
            )
        ]
        for tab_name in tabs:
            if tab_name not in CONTROL_TABS and not _adoption_tab_allowed(tab_name, allowed_tabs):
                logger.warning("waca-loop: ignoring unlisted Lite policy tab %s", tab_name)

        self._sources = sources
        self._guides = InMemoryPolicyGuideStore(
            guides=guides,
            floors=floors,
            safe_channels=safe_channels,
        )
        self._adoption = InMemoryAdoptionMapStore(adoption_rows)
        self._checksum = _checksum({
            "guides": [g.__dict__ for g in guides],
            "floors": [f.__dict__ for f in floors],
            "safe_channels": [c.__dict__ for c in safe_channels],
            "adoption_rows": [r.__dict__ for r in adoption_rows],
        })


def rows_from_grid(grid: Sequence[Sequence[object]]) -> list[dict[str, object]]:
    """Convert a tab grid to dict rows using the first non-empty row as header."""
    for i, row in enumerate(grid):
        header = [_norm(c) for c in row]
        if any(header):
            return [
                {header[j]: cell for j, cell in enumerate(raw) if j < len(header) and header[j]}
                for raw in grid[i + 1:]
                if any(_norm(c) for c in raw)
            ]
    return []


def _parse_sources(rows: Sequence[Mapping[str, object]]) -> dict[str, PolicySource]:
    out: dict[str, PolicySource] = {}
    for row in rows:
        source_id = _pick(row, "source_id")
        if not source_id:
            continue
        out[source_id] = PolicySource(
            source_id=source_id,
            source_type=_pick(row, "source_type") or "sheet",
            title=_pick(row, "title"),
            url=_pick(row, "url"),
            owner_unit=_pick(row, "owner_unit") or ORG_UNIT,
            refresh_frequency=_pick(row, "refresh_frequency") or None,
            next_refresh_at=_parse_dt(_pick(row, "next_refresh_at")),
            last_refreshed_at=_parse_dt(_pick(row, "last_refreshed_at")),
            source_version=_pick(row, "source_version") or None,
            checksum=_pick(row, "checksum") or None,
            status=_pick(row, "status") or "active",
            tab_name=_pick(row, "tab_name") or _pick(row, "sheet_name") or None,
        )
    return out


def _parse_guides(rows: Sequence[Mapping[str, object]]) -> list[PolicyGuide]:
    guides: list[PolicyGuide] = []
    for row in rows:
        guide_id = _pick(row, "guide_id")
        summary = _pick(row, "summary")
        if not guide_id or not summary:
            continue
        guides.append(PolicyGuide(
            guide_id=guide_id,
            source_id=_pick(row, "source_id") or guide_id,
            source_type=_pick(row, "source_type") or "internal_policy",
            title=_pick(row, "title") or guide_id,
            summary=summary,
            status=_status(_pick(row, "status"), default="trial"),
            version=_int(_pick(row, "version"), default=1),
            effective_at=_parse_dt(_pick(row, "effective_at")),
            review_frequency=_pick(row, "review_frequency") or None,
            source_ref=safe_source_ref("policy_guide", _pick(row, "source_ref")),
        ))
    return guides


def _parse_floors(rows: Sequence[Mapping[str, object]]) -> list[PolicyFloor]:
    floors: list[PolicyFloor] = []
    for row in rows:
        row_id = _pick(row, "row_id")
        line = _pick(row, "line")
        if not row_id or not line:
            continue
        floors.append(PolicyFloor(
            row_id=row_id,
            line=line,
            effect=_pick(row, "effect"),
            trigger_kinds=_split(_pick(row, "trigger_kinds")),
            source_ref=safe_source_ref("policy_floor", _pick(row, "source_ref")),
            status=_status(_pick(row, "status"), default="approved"),
            version=_int(_pick(row, "version"), default=1),
        ))
    return floors


def _parse_safe_channels(rows: Sequence[Mapping[str, object]]) -> list[SafeChannel]:
    channels: list[SafeChannel] = []
    for row in rows:
        channel_id = _pick(row, "channel_id")
        if not channel_id:
            continue
        channels.append(SafeChannel(
            channel_id=channel_id,
            scope_unit=_pick(row, "scope_unit") or ORG_UNIT,
            status=_pick(row, "status") or "unreviewed",
            allowed_data_kinds=_split(_pick(row, "allowed_data_kinds")),
            required_controls=_split(_pick(row, "required_controls")),
            approved_by_role=_pick(row, "approved_by_role") or None,
            approved_at=_parse_dt(_pick(row, "approved_at")),
            source_ref=safe_source_ref("safe_channel", _pick(row, "source_ref")),
        ))
    return channels


def _parse_adoption_rows(
    tab_name: str,
    rows: Sequence[Mapping[str, object]],
    unit_map: Mapping[str, str],
    workspace_domains: frozenset[str],
) -> list[AdoptionRow]:
    out: list[AdoptionRow] = []
    for pos, row in enumerate(rows, start=1):
        business = _pick_any(row, ADOPTION_COLUMNS["business"])
        if not business:
            continue
        current_raw = _pick_any(row, ADOPTION_COLUMNS["current_level"])
        try:
            current_level = _level(current_raw, row_label=f"{tab_name}:{pos}")
            target_level = _optional_level(
                _pick_any(row, ADOPTION_COLUMNS["target_level"]),
                row_label=f"{tab_name}:{pos}",
            )
        except ValueError as exc:
            logger.warning("waca-loop: dropping invalid Lite policy row: %s", exc)
            continue
        no = _pick_any(row, ADOPTION_COLUMNS["no"]) or str(pos)
        scope_unit = _scope_unit(tab_name, row, unit_map)
        actor_scope = _actor_scope(row, workspace_domains)
        if actor_scope == "":
            logger.warning("waca-loop: dropping Lite policy row with unsafe actor_scope")
            continue
        intent = {
            key: value for key, value in {
                "wanted_work": _pick_any(row, ADOPTION_COLUMNS["wanted_work"]),
                "try_next": _pick_any(row, ADOPTION_COLUMNS["try_next"]),
                "concern": _pick_any(row, ADOPTION_COLUMNS["concern"]),
                "improvement_request": _pick_any(row, ADOPTION_COLUMNS["improvement_request"]),
            }.items()
            if value
        }
        current_usage = _pick_any(row, ADOPTION_COLUMNS["current_usage"])
        out.append(AdoptionRow(
            row_id=_row_id(scope_unit, no, actor_scope),
            scope_unit=scope_unit,
            actor_scope=actor_scope,
            business=business,
            current_level=current_level,
            target_level=target_level,
            current_usage=current_usage,
            safe_practice=current_usage,
            issue_summary=_pick_any(row, ADOPTION_COLUMNS["issue_summary"]) or None,
            next_step=_pick_any(row, ADOPTION_COLUMNS["next_step"]) or None,
            owner_label=_pick_any(row, ADOPTION_COLUMNS["owner_label"]) or None,
            manager_guidance=_pick_any(row, ADOPTION_COLUMNS["manager_guidance"]) or None,
            person_intent=intent,
            share_scope=_share_scope(_pick_any(row, ADOPTION_COLUMNS["share_scope"]), actor_scope),
            updated_at=_parse_dt(_pick_any(row, ADOPTION_COLUMNS["updated_at"])),
            status=_status(_pick_any(row, ADOPTION_COLUMNS["status"]), default="active"),
            source_ref=safe_source_ref("sheet", f"sheet:{tab_name}#{no}"),
        ))
    return out


def _norm(value: object) -> str:
    return str(value or "").strip()


def _pick(row: Mapping[str, object], key: str) -> str:
    return _norm(row.get(key))


def _pick_any(row: Mapping[str, object], keys: Sequence[str]) -> str:
    for key in keys:
        value = _norm(row.get(key))
        if value:
            return value
    return ""


def _int(value: str, *, default: int) -> int:
    try:
        return int(value)
    except ValueError:
        return default


def _level(value: str, *, row_label: str) -> int:
    if value not in {"0", "1", "2"}:
        raise ValueError(f"{row_label}: 現状レベルが 0/1/2 でない")
    return int(value)


def _optional_level(value: str, *, row_label: str) -> int | None:
    if value in UNFILLED:
        return None
    return _level(value, row_label=row_label)


def _split(value: str) -> list[str]:
    return [
        part.strip() for chunk in value.split("\n")
        for part in chunk.split(",")
        if part.strip()
    ]


def _status(value: str, *, default: str) -> str:
    return default if value in UNFILLED else value


def _scope_unit(tab_name: str, row: Mapping[str, object], unit_map: Mapping[str, str]) -> str:
    raw = _pick_any(row, ADOPTION_COLUMNS["scope_unit"]) or tab_name
    return unit_map.get(raw) or unit_map.get(raw.lower()) or raw


def _actor_scope(row: Mapping[str, object], workspace_domains: frozenset[str]) -> str | None:
    raw = _pick_any(row, ADOPTION_COLUMNS["actor_scope"])
    if not raw:
        return None
    if raw.startswith(("sub:", "accounts.google.com:", "https://accounts.google.com")):
        return ""
    if raw.isdecimal() and len(raw) >= 15:
        return ""
    if "@" in raw:
        domain = raw.rsplit("@", 1)[1].lower()
        return raw if domain in workspace_domains else ""
    return raw if raw.startswith("person:") else f"person:{raw}"


def _row_id(scope_unit: str, no: str, actor_scope: str | None) -> str:
    if actor_scope:
        return f"{safe_source_ref('actor', actor_scope)}:{no}"
    return f"{scope_unit}:{no}"


def _share_scope(value: str, actor_scope: str | None) -> str:
    if value in UNFILLED:
        return "person" if actor_scope else "unit"
    if value in SHARE_SCOPE_ALIASES:
        return SHARE_SCOPE_ALIASES[value]
    logger.warning("waca-loop: unknown WACA Loop share scope %r; falling back to person", value)
    return "person"


def _parse_dt(value: str) -> datetime | None:
    if value in UNFILLED:
        return None
    normalized = value.replace("/", "-")
    try:
        return _aware_utc(datetime.fromisoformat(normalized))
    except ValueError:
        try:
            return _aware_utc(datetime.fromisoformat(normalized + "T00:00:00+00:00"))
        except ValueError:
            return None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _checksum(payload: object) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(body.encode("utf-8")).hexdigest()


class EmptySnapshotError(ValueError):
    """A read completed but did not contain enough policy data to replace last-good."""


def _adoption_tab_allowlist(sources: Mapping[str, PolicySource]) -> frozenset[str]:
    tabs = {
        s.tab_name or s.source_id
        for s in sources.values()
        if s.status in {"active", "trial", "approved"} and s.source_type in ADOPTION_SOURCE_TYPES
    }
    return frozenset(t for t in tabs if t)


def _adoption_tab_allowed(tab_name: str, allowed_tabs: frozenset[str]) -> bool:
    return tab_name not in CONTROL_TABS and tab_name in allowed_tabs


def _next_refresh_at(source: PolicySource) -> datetime | None:
    if source.next_refresh_at is not None:
        return source.next_refresh_at
    if source.refresh_frequency and source.last_refreshed_at is None:
        return datetime.min.replace(tzinfo=UTC)
    if source.last_refreshed_at is None:
        return None
    delta = _frequency_delta(source.refresh_frequency)
    return source.last_refreshed_at + delta if delta is not None else None


def _mark_refreshed(source: PolicySource, refreshed_at: datetime, checksum: str) -> PolicySource:
    delta = _frequency_delta(source.refresh_frequency)
    return PolicySource(
        source_id=source.source_id,
        source_type=source.source_type,
        title=source.title,
        url=source.url,
        owner_unit=source.owner_unit,
        refresh_frequency=source.refresh_frequency,
        next_refresh_at=refreshed_at + delta if delta is not None else None,
        last_refreshed_at=refreshed_at,
        source_version=source.source_version,
        checksum=checksum,
        status=source.status,
        tab_name=source.tab_name,
    )


def _frequency_delta(value: str | None) -> timedelta | None:
    if not value:
        return None
    v = value.strip().lower()
    aliases = {
        "daily": timedelta(days=1),
        "毎日": timedelta(days=1),
        "weekly": timedelta(days=7),
        "毎週": timedelta(days=7),
        "monthly": timedelta(days=30),
        "毎月": timedelta(days=30),
    }
    if v in aliases:
        return aliases[v]
    if v.endswith("d") and v[:-1].isdigit():
        return timedelta(days=int(v[:-1]))
    if v.endswith("日") and v[:-1].isdigit():
        return timedelta(days=int(v[:-1]))
    if v.endswith("w") and v[:-1].isdigit():
        return timedelta(days=7 * int(v[:-1]))
    return None


def _error_kind(exc: Exception) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if "403" in text or "permission" in text or "forbidden" in text:
        return "auth"
    if "429" in text or "quota" in text or "rate" in text:
        return "quota"
    return "transient"


__all__ = ["LiteSheetsPolicyStore", "PolicySource", "SheetPolicyReader", "rows_from_grid"]
