"""改善要望のストアと可視性 (docs/SPEC_improvement_requests.md §2・§3)。

`improvement_tasks` と**別立て**にしてある。あちらは申告・インシデント由来でスチュワードが
捌く導線、こちらは「WACA Loop という道具への注文」で開発者が捌く導線。寿命が違う（前者は組織の
記録として残り、後者は直れば役目が終わる）ので、同じ層に置かない。

可視性は §3.3 と同型の**二重ゲート** — in-process の :func:`request_visible_to` と
SQL の :func:`request_acl_where_clause` を必ず対で直すこと。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from waca_loop.models import (
    ImprovementRequest,
    RequestStatus,
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Viewer,
)
from waca_loop.retrieval.acl import (
    OWNER_UNIT,
    STEWARD_UNIT,
    acl_where_clause,
    visible_to,
)

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings

# `unverified` を見てよい人 — フォーム経由の未検証の受信箱 (§3.2)。
_TRIAGE_UNITS = frozenset({STEWARD_UNIT, OWNER_UNIT})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _request_to_row(r: ImprovementRequest, request_id: str, now: datetime) -> dict:
    """モデル → BQ 行。**priority / severity も逐語欄も無い**（§2・R6・INV-1）。"""
    return {
        "request_id": request_id,
        "kind": r.kind.value,
        "title": r.title,
        "body": r.body,
        "reporter_id": r.reporter_id,
        "reporter_label": r.reporter_label,
        "channel": r.channel,
        "status": r.status.value,
        "owner_unit": r.owner_unit,
        "scope_tier": r.scope.tier.value,
        "scope_audience": r.scope.audience.value,
        "scope_sensitivity": r.scope.sensitivity.value,
        "issue_url": r.issue_url,
        "resolution": r.resolution,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }


def _scope_of(row: dict) -> ScopeTags:
    """行の3軸スコープ。欠けていれば最も狭い側に倒す（fail closed）。"""
    return ScopeTags(
        ScopeTier(row.get("scope_tier") or ScopeTier.DEPT.value),
        ScopeAudience(row.get("scope_audience") or ScopeAudience.INTERNAL.value),
        ScopeSensitivity(row.get("scope_sensitivity") or ScopeSensitivity.NORMAL.value),
    )


def request_visible_to(row: dict, viewer: Viewer) -> bool:
    """この改善要望を ``viewer`` が見てよいか（in-process ゲート）。

    可視性の本体は **`retrieval/acl.visible_to` にそのまま任せる**。要望専用の ACL 方言を
    作らないのは、方言が増えるほど「どこかだけ緩い」が生まれるからである（§4e と同じ趣旨）。
    ここが足すのは、この層にしかない2つの事情だけ:

    - ``unverified``（ログイン不要フォーム経由・本人性なし）は**仕分ける人だけ**が見る。
      ここを緩めると、URL を知った外部の書き込みがそのまま全員の一覧に流れ込む。
    - スチュワードと WACA Loop オーナーは横断で見える。背骨を見るのが役割そのもの
      （`improvement/acl.py` の 横断視界 と同じ理屈）。
    """
    if not viewer.is_org_member:
        return False
    if viewer.units & _TRIAGE_UNITS:
        return True
    # status が欠けている行は unverified 扱い（fail closed）。SQL 側は NULL 比較が NULL に
    # なって除外されるので、そちらに向きを揃える — 片側だけ fail-open にしない。
    if (row.get("status") or RequestStatus.UNVERIFIED.value) == RequestStatus.UNVERIFIED.value:
        return False
    owner_unit = row.get("owner_unit") or None   # '' と None を同じに扱う（SQL 側と対）
    return visible_to(_scope_of(row), owner_unit, viewer)


def request_acl_where_clause(viewer: Viewer, *, alias: str = "r") -> tuple[str, dict]:
    """:func:`request_visible_to` と同じ規則の SQL 断片（禁止行を BQ から出さない・§3.3）。

    3軸の部分は `retrieval/acl.acl_where_clause` をそのまま使う（in-process 側が
    `visible_to` を使うのと対）。この層固有の2条件だけをここで足す。
    片方だけ直すと二重ゲートが二重でなくなる。必ず対で直すこと。
    """
    scope_sql, params = acl_where_clause(viewer, alias=alias)
    params["req_is_org_member"] = viewer.is_org_member
    # 仕分け役かどうかはパラメータで渡す（文字列に埋めない） — 埋めると「誰が仕分け役か」で
    # 2つのゲートが食い違いうる。
    params["req_is_triage"] = bool(viewer.units & _TRIAGE_UNITS)
    status_col = f"{alias}.status"
    sql = (
        "(@req_is_org_member AND (@req_is_triage OR ("
        f"{status_col} != '{RequestStatus.UNVERIFIED.value}' AND {scope_sql})))"
    )
    return sql, params


class InMemoryRequestStore:
    """オフライン/テスト用。BigQuery 版と同じ表面を持つ（ストアペア規約）。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, request: ImprovementRequest) -> str:
        request_id = request.request_id or str(uuid.uuid4())
        self.rows.append(_request_to_row(request, request_id, _utcnow()))
        return request_id

    def list(
        self,
        *,
        viewer: Viewer,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[dict]:
        """``viewer`` が見てよい要望。``viewer`` は必須（既定値を置かない）。

        既定 None にすると「ゲート無し」が呼び忘れ1つで成立し、しかも fail-OPEN になる。
        安全な道が楽な道であるべきなので、ここは必ず名指しさせる（tasks.py と同じ判断）。
        """
        out = self.rows
        if status:
            out = [r for r in out if r["status"] == status]
        if kind:
            out = [r for r in out if r["kind"] == kind]
        return [r for r in out if request_visible_to(r, viewer)]

    def get(self, request_id: str, viewer: Viewer) -> dict | None:
        return next(
            (
                r
                for r in self.rows
                if r["request_id"] == request_id and request_visible_to(r, viewer)
            ),
            None,
        )

    def set_status(
        self,
        request_id: str,
        status: RequestStatus,
        *,
        viewer: Viewer,
        resolution: str | None = None,
    ) -> None:
        """``viewer`` は BigQuery 双子が UPDATE の WHERE に使う（ストアペアの表面を揃える）。"""
        for r in self.rows:
            if r["request_id"] == request_id and not request_visible_to(r, viewer):
                raise ValueError(f"request not found or not visible: {request_id}")
        for r in self.rows:
            if r["request_id"] == request_id:
                r["status"] = status.value
                if resolution is not None:
                    r["resolution"] = resolution
                r["updated_at"] = _utcnow().isoformat()
                return
        raise ValueError(f"request not found: {request_id}")


class BigQueryRequestStore:
    """BigQuery 版。InMemory 版と同じ表面（ストアペア規約）。"""

    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def add(self, request: ImprovementRequest) -> str:
        request_id = request.request_id or str(uuid.uuid4())
        row = _request_to_row(request, request_id, _utcnow())
        errors = self._client.insert_rows_json(
            self._settings.improvement_requests_table, [row]
        )
        if errors:
            raise RuntimeError(f"improvement_requests insert failed: {errors}")
        return request_id

    def list(
        self,
        *,
        viewer: Viewer,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[dict]:
        from google.cloud import bigquery

        from waca_loop.bq import scalar_params

        clauses, params = [], []
        if status:
            clauses.append("r.status = @status")
            params.append(bigquery.ScalarQueryParameter("status", "STRING", status))
        if kind:
            clauses.append("r.kind = @kind")
            params.append(bigquery.ScalarQueryParameter("kind", "STRING", kind))
        acl_sql, acl_params = request_acl_where_clause(viewer, alias="r")
        clauses.append(acl_sql)
        params.extend(scalar_params(acl_params))
        where = " WHERE " + " AND ".join(clauses)
        sql = (
            f"SELECT r.* FROM `{self._settings.improvement_requests_table}` AS r{where} "
            "ORDER BY r.created_at DESC"
        )
        job = self._client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params)
        )
        return [dict(row) for row in job.result()]

    def get(self, request_id: str, viewer: Viewer) -> dict | None:
        rows = [r for r in self.list(viewer=viewer) if r["request_id"] == request_id]
        return rows[0] if rows else None

    def set_status(
        self,
        request_id: str,
        status: RequestStatus,
        *,
        viewer: Viewer,
        resolution: str | None = None,
    ) -> None:
        from google.cloud import bigquery

        sets = ["status = @status", "updated_at = CURRENT_TIMESTAMP()"]
        params = [
            bigquery.ScalarQueryParameter("status", "STRING", status.value),
            bigquery.ScalarQueryParameter("request_id", "STRING", request_id),
        ]
        if resolution is not None:
            sets.append("resolution = @resolution")
            params.append(
                bigquery.ScalarQueryParameter("resolution", "STRING", resolution)
            )
        # UPDATE 自体にも ACL を AND する。呼び出し側 (server.set_request_status) は事前に
        # get で可視性を確かめているが、それは TOCTOU で、しかもストアを直接呼ぶ将来の経路には
        # 効かない。二重ゲートの原則どおり、書き込み側にも同じ規則を置く。
        from waca_loop.bq import scalar_params

        acl_sql, acl_params = request_acl_where_clause(viewer, alias="r")
        params.extend(scalar_params(acl_params))
        sql = (
            f"UPDATE `{self._settings.improvement_requests_table}` AS r "
            f"SET {', '.join(sets)} WHERE r.request_id = @request_id AND {acl_sql}"
        )
        self._client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params)
        ).result()
