"""Persisted session-scope declarations (SPEC_ops R1).

A declaration held only in a process-wide dict would make **restart an uncontrolled expiry**:
a deploy or cold start would silently reset everyone to ``unknown``, so a conversation declared
``private`` could drift back to being recorded with no user action. That is fail-open.

Persisting it here makes the TTL (``guard.scope_expired``) the *only* rule that ends a
declaration — one predictable expiry instead of two competing ones.

Store pair, same surface on both sides (repo convention): tests inject
:class:`InMemorySessionScopeStore`, ``build_shared_runtime`` wires the BigQuery twin. Nothing
here is imported at module level from ``google.cloud`` — the suite stays offline.

**Attribution follows §5**: resolved viewers key on ``actor_id``, unresolved on the hashed
``actor_raw``. The raw OAuth ``sub`` is never written. **No reason, no content** (R1): the row
is who / which scope / when, and nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from waca_loop.guard import scope_key

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: object) -> datetime | None:
    """Coerce whatever the backend hands back into a tz-aware UTC datetime.

    ``declared_at`` is the **entire input to the TTL**, and `guard.scope_expired` fails closed
    on anything that is not tz-aware — meaning a string or a naive datetime silently makes the
    declaration immortal. Real BigQuery returns aware datetimes for TIMESTAMP, but the twin
    must not depend on that being true of every backend it is ever run against (the offline
    SQLite twin returns a string).
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


# "同じ actor" の定義は純ロジック側 (guard.scope_key) が持つ。ここで別に書くと、
# 両側が別々の鍵を使うことになる。


class InMemorySessionScopeStore:
    """In-memory declarations (tests / stdio)."""

    def __init__(self) -> None:
        # Keyed by the §5 identity pair, never the raw subject.
        self.rows: dict[tuple[str | None, str | None], dict] = {}

    def upsert(
        self,
        *,
        actor_id: str | None,
        actor_raw: str | None,
        scope: str,
        declared_at: datetime | None = None,
    ) -> None:
        now = declared_at or _utcnow()
        actor_id, actor_raw = scope_key(actor_id, actor_raw)
        self.rows[(actor_id, actor_raw)] = {
            "actor_id": actor_id,
            "actor_raw": actor_raw,
            "scope": scope,
            "declared_at": now,
            "updated_at": now,
        }

    def get(
        self, *, actor_id: str | None, actor_raw: str | None
    ) -> tuple[str, datetime] | None:
        """``(scope, declared_at)`` for this actor, or ``None`` if never declared."""
        row = self.rows.get(scope_key(actor_id, actor_raw))
        if row is None:
            return None
        return row["scope"], row["declared_at"]


class BigQuerySessionScopeStore:
    """BigQuery twin of :class:`InMemorySessionScopeStore`.

    **Insert-only, read-newest.** DELETE-then-INSERT (one row per actor) would break in
    production, destructively. BigQuery rejects DML against rows
    still in the streaming buffer (~30–90 min) — the same constraint this repo already records
    at ``ledger/usage_log.py`` and ``knowledge/store.py`` — and **re-declaring within that
    window is the normal case**, because the refusal text tells people to do exactly that. The
    DELETE would raise, the INSERT would never run, and the store would keep the *previous*
    scope while memory held the new one: declare ``org`` at 09:10, switch to ``private`` at
    09:25, restart at 10:00, and the exclusion is gone. The reverse order is worse still —
    DELETE succeeding and INSERT failing leaves no row where an exclusion should be.

    So: append, and let ``get`` read the newest (``client_store.py`` is insert-only for exactly
    this reason). A failed write leaves the previous row standing rather than destroying it,
    which is the fail-closed direction. The table is therefore a **history** of declarations,
    not current state — see the retention note in ``sql/ddl/13_session_scopes.sql``.
    """

    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def upsert(
        self,
        *,
        actor_id: str | None,
        actor_raw: str | None,
        scope: str,
        declared_at: datetime | None = None,
    ) -> None:
        """Append the declaration. ``get`` reads the newest — see the class docstring."""
        actor_id, actor_raw = scope_key(actor_id, actor_raw)
        now = (declared_at or _utcnow()).isoformat()
        errors = self._client.insert_rows_json(
            self._settings.session_scopes_table,
            [{
                "actor_id": actor_id,
                "actor_raw": actor_raw,
                "scope": scope,
                "declared_at": now,
                "updated_at": now,
            }],
        )
        if errors:
            raise RuntimeError(f"session_scopes insert failed: {errors}")

    def get(
        self, *, actor_id: str | None, actor_raw: str | None
    ) -> tuple[str, datetime] | None:
        from google.cloud import bigquery

        job = self._client.query(
            f"""
            SELECT scope, declared_at
            FROM `{self._settings.session_scopes_table}`
            WHERE (@actor_id IS NOT NULL AND actor_id = @actor_id)
               OR (@actor_id IS NULL AND @actor_raw IS NOT NULL AND actor_raw = @actor_raw)
            ORDER BY declared_at DESC
            LIMIT 1
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("actor_id", "STRING", actor_id),
                    bigquery.ScalarQueryParameter("actor_raw", "STRING", actor_raw),
                ]
            ),
        )
        for row in job.result():
            declared_at = _as_utc(row["declared_at"])
            if declared_at is None:
                # 読めない時刻を「無い」として返すと guard 側は失効させない＝締まる側。
                return row["scope"], _utcnow()
            return row["scope"], declared_at
        return None
