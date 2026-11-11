"""Layer-1 ledger writes (§2.2 ③, §4.2 log_usage).

Records a *summary only* — the row schema has no verbatim-prompt column, so raw prompts
cannot be persisted here even by mistake (enforces T6 / 要約のみ).

**R6 withdrawal — append-only, not an UPDATE.**
A person who notices their private or other-org content landed here withdraws it themselves.
The withdrawal is a **row appended to ``usage_withdrawals``**, never an edit of the ledger row:
BigQuery refuses DML against rows still in the streaming buffer (~30–90 min), and R6's whole
use case is "the AI just logged my private conversation" — i.e. always inside that window.
An `UPDATE`-based design would fail exactly when it is needed, and only in production, with
the offline suite green. Appending has no such restriction, so
**the withdrawal takes effect immediately**. Three properties this file owns:

* **Only the owner can withdraw.** The actor match is part of the statement that writes the
  withdrawal, not an argument a caller could widen — same rule and shape on both twins.
* **Withdrawn means withdrawn everywhere.** Every read over ``usage_log`` anti-joins this
  table. ``scripts/coverage_report.py`` does the same for the operator console.
* **The ledger row survives** (R6). It stops being visible; it is not destroyed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from waca_loop.ledger.tool_events import row_ts
from waca_loop.models import UsageRecord

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings


def new_log_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True)
class UsageSummary:
    """Summary-only ledger read row for PolicyWorkbench.

    This is a read surface, not a new write model. It deliberately mirrors the safe subset
    of ``usage_log`` and never grows raw prompt / transcript / verbatim fields.
    """

    log_id: str
    occurred_at: datetime
    actor_id: str | None
    owner_unit: str | None
    use_category: str
    data_kinds: list[str]
    summary: str
    policy_row_ids: list[str]

    def as_dict(self) -> dict:
        return {
            "log_id": self.log_id,
            "occurred_at": self.occurred_at.isoformat(),
            "actor_id": self.actor_id,
            "owner_unit": self.owner_unit,
            "use_category": self.use_category,
            "data_kinds": list(self.data_kinds),
            "summary": self.summary,
            "policy_row_ids": list(self.policy_row_ids),
        }


def _row_to_usage_summary(row: dict) -> UsageSummary:
    occurred = row.get("occurred_at")
    if isinstance(occurred, datetime):
        occurred_at = occurred
    else:
        occurred_at = datetime.fromisoformat(str(occurred))
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    return UsageSummary(
        log_id=str(row["log_id"]),
        occurred_at=occurred_at.astimezone(UTC),
        actor_id=row.get("actor_id"),
        owner_unit=row.get("owner_unit"),
        use_category=str(row.get("use_category", "")),
        data_kinds=list(row.get("data_kinds") or []),
        summary=str(row.get("summary") or ""),
        policy_row_ids=list(row.get("policy_row_ids") or []),
    )


def _can_read_actor_summaries(viewer, actor_id: str) -> bool:
    if viewer.person_id and viewer.person_id == actor_id:
        return True
    from waca_loop.retrieval.acl import STEWARD_UNIT

    return STEWARD_UNIT in viewer.units


#: Closed set of withdrawal reasons. Deliberately **not** free text: the row being withdrawn
#: is one that already holds content it should not, and a free-text "why" invites the person
#: to type the very private detail again into the same table (FLOOR-01). A code is enough for
#: the steward to triage, and carries nothing new.
DELETION_REASONS = ("private", "other_org", "mistake")

#: Columns :meth:`list_withdrawn` returns, on BOTH stores. Written once because the twins have
#: to agree: a test that passes against the in-memory store while production quietly returns a
#: different shape is the drift this project's store pattern exists to prevent. Narrower than
#: the row on purpose — the steward needs to identify and triage a withdrawn record, not to
#: re-derive everything about it (``actor_raw`` and the policy anchors are left out).
#: ``summary`` is deliberately ABSENT. These rows are, by construction, the summaries people
#: looked at and decided were too private to keep; a list tool that hands the newest ones into
#: an external model's context on demand is the wrong shape for that.
#: Identify and triage here; read one row's content through :meth:`get_withdrawn` — which is
#: trailed fail-closed with the specific ``log_id``, so "which private summary did the steward
#: read" has an answer afterwards.
#: ``reason`` is absent too: ``other_org`` sitting next to an ``actor_id``, unbounded in time,
#: turns the triage list into "who has been doing another organisation's work on the clock,
#: with dates" — a report R6 does not sanction. R6 sanctions *investigation*, not browsing.
_WITHDRAWN_FIELDS = (
    "log_id", "occurred_at", "actor_id", "client", "use_category", "withdrawn_at",
)
#: What :meth:`get_withdrawn` adds — the content, and why it was withdrawn. That read is
#: trailed fail-closed with the ``log_id``, so this pairing always has a name against it.
_WITHDRAWN_DETAIL_FIELDS = (*_WITHDRAWN_FIELDS, "reason", "summary")


def _record_to_row(record: UsageRecord, log_id: str, occurred_at: datetime) -> dict:
    """Map a :class:`UsageRecord` to a ``usage_log`` row. Only ``summary`` carries content."""
    return {
        "log_id": log_id,
        "occurred_at": occurred_at.isoformat(),
        "actor_id": record.actor_id,
        "actor_raw": record.actor_raw,
        "client": record.client,
        "use_category": record.use_category.value,
        "data_kinds": list(record.data_kinds),
        "summary": record.summary,
        "request_id": record.request_id,
        "ingested_via": record.ingested_via,
        # REPEATED column: BigQuery streaming inserts reject None — always a list
        # (omitting policy_row_ids would fail the whole insert).
        "policy_row_ids": list(record.policy_row_ids) if record.policy_row_ids else [],
        "policy_version": record.policy_version,
        "status": record.status,
        "created_at": occurred_at.isoformat(),
    }


#: How long a resend can arrive late and still be recognised.
#: **Minutes, not days, on purpose.** ``request_id`` is documented as optional (§4.2) and no
#: client was ever told it must be unique per turn, so a client that reuses one id across a
#: session must not have its later turns swallowed — that would *create* the unrecorded turns
#: the de-duplication exists to prevent. A retry happens seconds after the
#: failure it retries, so a short window keeps the protection and bounds the damage.
RESEND_WINDOW_MINUTES = 15


def _same_ledger_actor(row: dict, actor_id: str | None, actor_raw: str | None) -> bool:
    """In-process twin of the actor predicate in :meth:`BigQueryUsageLogStore._existing_log_id`.

    Falsy-not-``None`` for the same reason as :func:`_owns` and ``tool_events.same_actor``:
    "no identity" must never form a bucket that two callers could share — otherwise one
    unresolved client's ``request_id`` could suppress another's write.
    """
    if actor_id:
        return row.get("actor_id") == actor_id
    return bool(actor_raw) and row.get("actor_raw") == actor_raw


#: What "the same write" compares — every declared axis a retry reproduces byte-for-byte, so
#: any difference means a new turn rather than a resend. ``data_kinds`` / ``client`` /
#: ``status`` are in here because they are *declarations*: two turns with an identical summary
#: where the second one declares ``pii`` are not the same record, and folding the second away
#: would drop exactly the declaration that matters — and a `pending` first row must not
#: swallow a later `recorded` one.
_SAME_WRITE_FIELDS = ("use_category", "summary", "data_kinds", "client", "status")

#: SQL that rebuilds :func:`content_fingerprint` from a ``usage_log`` row. Kept next to the
#: Python side so the pair cannot drift — if one changes, the parity test goes red.
_FINGERPRINT_SQL = (
    "TO_BASE64(SHA256(CONCAT("
    "IFNULL(use_category, ''), '\n', IFNULL(summary, ''), '\n',"
    "TO_JSON_STRING(data_kinds), '\n', IFNULL(client, ''), '\n', IFNULL(status, ''))))"
)


def _kinds_json(kinds) -> str:
    """The same text BigQuery's ``TO_JSON_STRING(ARRAY<STRING>)`` produces for this list."""
    return json.dumps([str(k) for k in (kinds or [])], ensure_ascii=False,
                      separators=(",", ":"))


def content_fingerprint(
    *, use_category: str | None, summary: str | None, data_kinds, client: str | None,
    status: str | None,
) -> str:
    """A digest of the declared content — **what the resend lookup sends, instead of the text**.

    Sending ``summary`` as a *query parameter* would be a new exposure, not a copy of the
    insert: a query job persists its configuration, and ``jobs.get`` hands back
    ``queryParameters`` **values** to anyone holding ``bigquery.jobs.listAll`` on the project
    — a different, ungated population from the viewer-ACL'd, withdrawal-anti-joined readers of
    ``usage_log``. A digest answers "is this the same
    write?" without putting the content anywhere new.
    """
    joined = "\n".join([
        use_category or "", summary or "", _kinds_json(data_kinds), client or "", status or "",
    ])
    return base64.b64encode(hashlib.sha256(joined.encode()).digest()).decode()


def _record_fingerprint(record: UsageRecord) -> str:
    return content_fingerprint(
        use_category=record.use_category.value, summary=record.summary,
        data_kinds=record.data_kinds, client=record.client, status=record.status,
    )


def _same_content(row: dict, record: UsageRecord) -> bool:
    """True when the row IS this record, not merely a row sharing its ``request_id``.

    The second half of the request_id de-duplication. Compared through the same fingerprint the SQL
    twin
    rebuilds, so the two gates cannot answer differently; content is never logged or returned
    — this predicate only ever yields a boolean.
    """
    return content_fingerprint(
        use_category=row.get("use_category"), summary=row.get("summary"),
        data_kinds=row.get("data_kinds"), client=row.get("client"),
        status=row.get("status"),
    ) == _record_fingerprint(record)


def _owns(
    row: dict, actor_id: str | None, actor_raw: str | None, subject_hash: str | None
) -> bool:
    """In-process twin of the SQL ownership predicate in :meth:`BigQueryUsageLogStore.withdraw`.

    Falsy-not-``None`` on purpose, matching ``tool_events.same_actor``: "no identity" and
    "identity ''" must never form a bucket two callers could share. The SQL side spells the
    same rule with ``IS NOT NULL AND != ''`` so the two cannot drift.
    """
    if actor_id and row.get("actor_id") == actor_id:
        return True
    if actor_raw and row.get("actor_raw") == actor_raw:
        return True
    # Rows written before this person's `sub` was bound to a person_id.
    return bool(subject_hash) and row.get("actor_raw") == subject_hash


def _withdrawal_row(
    log_id: str, actor_id: str | None, actor_raw: str | None, reason: str, at: datetime
) -> dict:
    """A ``usage_withdrawals`` row. Carries the id and the code — never any content."""
    return {
        "withdrawal_id": str(uuid.uuid4()),
        "log_id": log_id,
        "actor_id": actor_id,
        "actor_raw": actor_raw,
        "reason": reason,
        "withdrawn_at": at.isoformat(),
        "created_at": at.isoformat(),
    }


class InMemoryUsageLogStore:
    """In-memory ledger (tests / offline). Exposes the same surface as the BQ store."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        #: Append-only R6 withdrawals — the in-memory twin of `usage_withdrawals`.
        self.withdrawals: list[dict] = []

    def record(
        self, record: UsageRecord, *, occurred_at: datetime | None = None, dedupe: bool = True
    ) -> str:
        at = occurred_at or _utcnow()
        if dedupe:                                     # resend de-duplication
            existing = self.existing_log_id(record, now=at)
            if existing is not None:
                return existing
        log_id = new_log_id()
        row = _record_to_row(record, log_id, at)
        self.rows.append(row)
        return log_id

    def existing_log_id(
        self, record: UsageRecord, *, now: datetime | None = None
    ) -> str | None:
        """The ``log_id`` this exact record already has, if any (§4.2).

        The write-failure message tells a client whose ledger write failed to **resend with the same
        ``request_id``**; without this, a client that retries after a write that actually
        landed (a timeout on the way back, say) doubles the row. Three conditions, all
        required: same actor, same ``request_id``, same content, inside
        :data:`RESEND_WINDOW_MINUTES`. Withdrawn rows are deliberately still matched — a
        resend must not be a way to re-write content its owner has already retracted (R6) —
        so the caller is told ``deduplicated`` rather than being told it wrote a fresh row.
        """
        if not record.request_id:
            return None
        at = now or _utcnow()
        cutoff = at - timedelta(minutes=RESEND_WINDOW_MINUTES)
        hits = []
        for row in self.rows:
            if row.get("request_id") != record.request_id:
                continue
            if not _same_ledger_actor(row, record.actor_id, record.actor_raw):
                continue
            if not _same_content(row, record):
                continue
            ts = row_ts(row)
            if ts is not None and ts >= cutoff:
                hits.append((ts, str(row["log_id"])))
        if not hits:
            return None
        # Deterministic, and the same rule as the twin's ``ORDER BY occurred_at DESC,
        # log_id DESC LIMIT 1`` — insertion order would diverge whenever a caller passes an
        # explicit ``occurred_at`` out of sequence.
        return max(hits)[1]

    def recent_summaries_for_actor(
        self, viewer, actor_id: str, *, limit: int = 20
    ) -> list[UsageSummary]:
        """Return recent summary-only rows for one actor.

        Caller identity is part of the method signature so no read path can ask for another
        actor's ledger by id alone. Withdrawn rows are excluded just like every other read.
        """
        if limit <= 0 or not _can_read_actor_summaries(viewer, actor_id):
            return []
        withdrawn = self._withdrawn_ids()
        rows = [
            row for row in self.rows
            if row["log_id"] not in withdrawn and row.get("actor_id") == actor_id
        ]
        rows.sort(key=lambda row: str(row.get("occurred_at", "")), reverse=True)
        return [_row_to_usage_summary(row) for row in rows[:limit]]

    def distinct_actors(self, window_days: int) -> set[str]:
        from datetime import timedelta

        cutoff = _utcnow() - timedelta(days=window_days)
        withdrawn = self._withdrawn_ids()
        seen: set[str] = set()
        for row in self.rows:
            if row["log_id"] in withdrawn:   # R6: a withdrawn row is not evidence of anything
                continue
            ts = datetime.fromisoformat(row["occurred_at"])
            if ts >= cutoff and row["actor_id"]:
                seen.add(row["actor_id"])
        return seen

    def set_status(self, log_id: str, status: str) -> bool:
        """Steward transition for pending rows (SPEC_ops R2): approve → ``recorded``."""
        withdrawn = self._withdrawn_ids()
        for row in self.rows:
            if row["log_id"] == log_id and log_id not in withdrawn:
                row["status"] = status
                return True
        return False

    def delete(self, log_id: str) -> bool:
        """Steward discard (SPEC_ops R2) — the pending content is physically removed.

        Refuses a withdrawn row. The two paths collide in an ordinary way: a row goes
        ``pending``, the owner withdraws it, and the steward — working from a queue listed
        before that — clicks discard. Physically deleting it would break R6 ("行は残る")
        through a race nobody chose, and destroy the one copy the incident path
        is supposed to be able to reach.
        """
        if log_id in self._withdrawn_ids():
            return False
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["log_id"] != log_id]
        return len(self.rows) < before

    # --- R6 withdrawal ---------------------------------------------------------------------

    def _withdrawn_ids(self) -> set[str]:
        return {w["log_id"] for w in self.withdrawals}

    def withdraw(
        self,
        log_id: str,
        *,
        actor_id: str | None,
        actor_raw: str | None,
        subject_hash: str | None = None,
        reason: str,
        at: datetime | None = None,
    ) -> bool:
        """R6: the *owner* withdraws their own row. False if it isn't theirs.

        Not-yours and not-there return the same ``False`` on purpose: a caller must not be
        able to probe whose ``log_id`` exists by the shape of the answer.

        ``subject_hash`` is the caller's own hashed OAuth subject. It exists because rows
        written *before* a person's ``sub`` was bound to a ``person_id`` carry
        ``actor_id=NULL, actor_raw=<that hash>`` — without this they would be permanently
        un-withdrawable for exactly the population whose attribution was shakiest. It is still
        strictly owner-scoped: the same Google subject.
        """
        if reason not in DELETION_REASONS:      # enforced on both sides (§3.3 の流儀)
            raise ValueError(f"reason must be one of {'|'.join(DELETION_REASONS)}")
        if log_id in self._withdrawn_ids():
            return False
        for row in self.rows:
            if row["log_id"] != log_id:
                continue
            if not _owns(row, actor_id, actor_raw, subject_hash):
                return False
            self.withdrawals.append(
                _withdrawal_row(log_id, actor_id, actor_raw, reason, at or _utcnow())
            )
            return True
        return False

    def _withdrawn_join(self, limit: int, fields: tuple[str, ...]) -> list[dict]:
        by_id = {r["log_id"]: r for r in self.rows}
        # log_id ごとに最初の1件だけ（BigQuery 側の GROUP BY と同じ意味）。
        first: dict[str, dict] = {}
        for w in sorted(self.withdrawals, key=lambda w: str(w["withdrawn_at"])):
            first.setdefault(w["log_id"], w)
        out = []
        for w in sorted(
            first.values(), key=lambda w: (str(w["withdrawn_at"]), w["log_id"]), reverse=True
        ):
            row = by_id.get(w["log_id"])
            if row is None:          # discarded physically afterwards — nothing left to show
                continue
            merged = {**row, "withdrawn_at": w["withdrawn_at"], "reason": w["reason"]}
            out.append({k: merged.get(k) for k in fields})
            if len(out) >= limit:
                break
        return out

    def list_withdrawn(self, limit: int) -> list[dict]:
        """Steward-only triage view (R6) — newest first, **without ``summary``**."""
        return self._withdrawn_join(limit, _WITHDRAWN_FIELDS)

    def get_withdrawn(self, log_id: str) -> dict | None:
        """Steward-only single row **with** ``summary`` — the investigation step (R6)."""
        for row in self._withdrawn_join(len(self.withdrawals) or 1, _WITHDRAWN_DETAIL_FIELDS):
            if row["log_id"] == log_id:
                return row
        return None


class BigQueryUsageLogStore:
    """Production ledger backed by BigQuery streaming inserts (§2.2 ③)."""

    # Lower bound for the backward partition scan. Rows older than this are never searched;
    # loading older rows requires lowering it (with tests).
    _scan_floor = datetime(2026, 1, 1, tzinfo=UTC)
    _min_partition_window_days = 365

    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def record(
        self, record: UsageRecord, *, occurred_at: datetime | None = None, dedupe: bool = True
    ) -> str:
        at = occurred_at or _utcnow()
        if dedupe:                                     # resend de-duplication
            existing = self.existing_log_id(record, now=at)
            if existing is not None:
                return existing
        log_id = new_log_id()
        row = _record_to_row(record, log_id, at)
        errors = self._client.insert_rows_json(self._settings.usage_log_table, [row])
        if errors:
            raise RuntimeError(f"usage_log insert failed: {errors}")
        return log_id

    def existing_log_id(
        self, record: UsageRecord, *, now: datetime | None = None
    ) -> str | None:
        """BigQuery twin of :meth:`InMemoryUsageLogStore.existing_log_id` (§4.2).

        A SELECT before the insert, not a MERGE: the table is fed by streaming inserts, and
        a row inserted seconds ago *is* visible to a query (only DML against it is
        restricted), which is exactly the window a resend lives in. The actor predicate is
        bound as a parameter and pinned to one actor, so no other actor's ``log_id`` can be
        handed back (§3.3 の流儀).
        """
        if not record.request_id:
            return None
        from google.cloud import bigquery

        at = now or _utcnow()
        job = self._client.query(
            f"""
            SELECT log_id
            FROM `{self._settings.usage_log_table}`
            -- withdrawal-exclusion-waived: this read returns no content, only the id a
            -- resend should reuse. Matching a withdrawn row is the protective answer —
            -- excluding it would let a retry re-write content its owner retracted (R6).
            WHERE request_id = @request_id
              -- PARTITION BY DATE(occurred_at): without this every write full-scans the
              -- ledger, so recording would get dearer as the org records more.
              AND occurred_at >= TIMESTAMP_SUB(@now, INTERVAL {int(RESEND_WINDOW_MINUTES)} MINUTE)
              -- NULLIF('' ) — falsy, not NULL, exactly like ``_same_ledger_actor``. With a
              -- plain IS NOT NULL, ``actor_id = ''`` matches and every empty-identity caller
              -- shares one dedup bucket, so one caller's request_id could suppress
              -- another's write.
              AND ((NULLIF(@actor_id, '') IS NOT NULL AND actor_id = @actor_id)
                OR (NULLIF(@actor_id, '') IS NULL AND NULLIF(@actor_raw, '') IS NOT NULL
                    AND actor_raw = @actor_raw))
              -- Same content, not merely the same id: a client that reuses one request_id
              -- across turns must still get every turn recorded. Compared as a
              -- digest so the summary text never becomes query-job metadata.
              AND {_FINGERPRINT_SQL} = @fingerprint
            ORDER BY occurred_at DESC, log_id DESC
            LIMIT 1
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("request_id", "STRING", record.request_id),
                    bigquery.ScalarQueryParameter("actor_id", "STRING", record.actor_id),
                    bigquery.ScalarQueryParameter("actor_raw", "STRING", record.actor_raw),
                    bigquery.ScalarQueryParameter("now", "TIMESTAMP", at),
                    bigquery.ScalarQueryParameter(
                        "fingerprint", "STRING", _record_fingerprint(record)
                    ),
                ]
            ),
        )
        for row in job.result():
            return dict(row)["log_id"]
        return None

    def recent_summaries_for_actor(
        self, viewer, actor_id: str, *, limit: int = 20
    ) -> list[UsageSummary]:
        """Return recent summary-only rows for one actor."""
        if limit <= 0:
            return []
        from google.cloud import bigquery

        from waca_loop.retrieval.acl import person_where_clause

        person_gate, acl_params = person_where_clause(viewer, alias="p")

        sql = f"""
            SELECT
              l.log_id,
              l.occurred_at,
              l.actor_id,
              NULL AS owner_unit,
              l.use_category,
              l.data_kinds,
              l.summary,
              l.policy_row_ids
            FROM `{self._settings.usage_log_table}` AS l
            WHERE l.actor_id = @actor_id
              AND l.{self._not_withdrawn}
              AND EXISTS (
                SELECT 1
                FROM `{self._settings.persons_table}` AS p
                WHERE p.person_id = l.actor_id
                  AND ({person_gate})
              )
            ORDER BY l.occurred_at DESC, l.log_id DESC
            LIMIT @limit
        """
        params = [
            bigquery.ScalarQueryParameter("actor_id", "STRING", actor_id),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        for name, value in acl_params.items():
            if isinstance(value, list | tuple | set | frozenset):
                params.append(bigquery.ArrayQueryParameter(name, "STRING", sorted(value)))
            elif isinstance(value, bool):
                params.append(bigquery.ScalarQueryParameter(name, "BOOL", value))
            else:
                params.append(bigquery.ScalarQueryParameter(name, "STRING", value))
        rows = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        ).result()
        return [
            summary for summary in (_row_to_usage_summary(dict(row)) for row in rows)
            if _can_read_actor_summaries(viewer, summary.actor_id or "")
        ]

    @property
    def _not_withdrawn(self) -> str:
        """SQL fragment: this ledger row has not been withdrawn (R6).

        One string, used by every read, so a new reader cannot quietly forget it. The twin of
        ``InMemoryUsageLogStore._withdrawn_ids``.
        """
        return (
            "log_id NOT IN (SELECT log_id FROM "
            f"`{self._settings.usage_withdrawals_table}`)"
        )

    def distinct_actors(self, window_days: int) -> set[str]:
        from google.cloud import bigquery

        sql = f"""
            SELECT DISTINCT actor_id
            FROM `{self._settings.usage_log_table}`
            WHERE actor_id IS NOT NULL
              AND {self._not_withdrawn}
              AND occurred_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @win DAY)
        """
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("win", "INT64", window_days)]
            ),
        )
        return {row["actor_id"] for row in job.result()}

    def set_status(self, log_id: str, status: str) -> bool:
        """Steward transition for pending rows (SPEC_ops R2).

        NOTE: rows still in the streaming buffer (~the first 30 min) reject DML; the steward
        review flow is asynchronous by nature, so in practice the row has settled by then.
        (R6 withdrawal deliberately does NOT use DML for this reason — see the module
        docstring.)
        """
        from google.cloud import bigquery

        sql = (
            f"UPDATE `{self._settings.usage_log_table}` SET status = @st "
            f"WHERE log_id = @id AND {self._not_withdrawn}"
        )
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("st", "STRING", status),
                    bigquery.ScalarQueryParameter("id", "STRING", log_id),
                ]
            ),
        )
        job.result()
        return (job.num_dml_affected_rows or 0) > 0

    def delete(self, log_id: str) -> bool:
        """Steward discard (SPEC_ops R2) — physical delete, but never of a withdrawn row."""
        from google.cloud import bigquery

        sql = (
            f"DELETE FROM `{self._settings.usage_log_table}` "
            f"WHERE log_id = @id AND {self._not_withdrawn}"
        )
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("id", "STRING", log_id)]
            ),
        )
        job.result()
        return (job.num_dml_affected_rows or 0) > 0

    # --- R6 withdrawal ---------------------------------------------------------------------

    def withdraw(
        self,
        log_id: str,
        *,
        actor_id: str | None,
        actor_raw: str | None,
        subject_hash: str | None = None,
        reason: str,
        at: datetime | None = None,
    ) -> bool:
        """R6 twin of :meth:`InMemoryUsageLogStore.withdraw` — ownership lives in the WHERE.

        ``INSERT … SELECT`` rather than a client-side insert: the ownership predicate then runs
        *inside* the statement, so a row belonging to someone else is not merely hidden from
        the answer — no withdrawal is written at all. Reading the ledger row is a SELECT, which
        the streaming buffer permits; nothing here is DML against ``usage_log``, which is the
        whole point (module docstring).
        """
        from google.cloud import bigquery

        if reason not in DELETION_REASONS:      # enforced on both sides (§3.3 の流儀)
            raise ValueError(f"reason must be one of {'|'.join(DELETION_REASONS)}")
        stamp = at or _utcnow()
        sql = f"""
            INSERT INTO `{self._settings.usage_withdrawals_table}`
                (withdrawal_id, log_id, actor_id, actor_raw, reason, withdrawn_at, created_at)
            SELECT @wid, l.log_id, @actor_id, @actor_raw, @reason, @at, @at
            FROM `{self._settings.usage_log_table}` AS l
            WHERE l.log_id = @id
              AND ((@actor_id IS NOT NULL AND @actor_id != '' AND l.actor_id = @actor_id)
                OR (@actor_raw IS NOT NULL AND @actor_raw != '' AND l.actor_raw = @actor_raw)
                OR (@subject_hash IS NOT NULL AND @subject_hash != ''
                    AND l.actor_raw = @subject_hash))
              AND l.log_id NOT IN (
                    SELECT log_id FROM `{self._settings.usage_withdrawals_table}`)
            LIMIT 1
        """
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("wid", "STRING", str(uuid.uuid4())),
                    bigquery.ScalarQueryParameter("id", "STRING", log_id),
                    bigquery.ScalarQueryParameter("actor_id", "STRING", actor_id),
                    bigquery.ScalarQueryParameter("actor_raw", "STRING", actor_raw),
                    bigquery.ScalarQueryParameter("subject_hash", "STRING", subject_hash),
                    bigquery.ScalarQueryParameter("reason", "STRING", reason),
                    bigquery.ScalarQueryParameter("at", "TIMESTAMP", stamp.isoformat()),
                ]
            ),
        )
        job.result()
        return (job.num_dml_affected_rows or 0) > 0

    def _partition_windows(self):
        days = max(self._min_partition_window_days, int(self._settings.usage_log_retention_days))
        end = _utcnow()
        while end > self._scan_floor:
            start = max(self._scan_floor, end - timedelta(days=days))
            yield start, end
            end = start

    def _withdrawal_refs_sql(self, *, one: bool) -> str:
        by_id = "AND log_id = @id" if one else ""
        limit = "LIMIT @scan_limit OFFSET @scan_offset" if not one else "LIMIT 1"
        # One row per log_id, not per withdrawal. Two concurrent INSERT…SELECT for the same
        # id can both pass the NOT IN (each reads a snapshot taken before the other commits),
        # so duplicates are possible even though hiding is unaffected (NOT IN is set
        # semantics). Without this, someone firing ten concurrent withdrawals at their OWN row
        # fills the steward's newest-first list — capped at 10 — with one id and pushes other
        # people's withdrawn rows out of the only listing view. That is oversight evasion, and
        # the in-memory twin cannot reproduce it, so no offline test would ever show it.
        return f"""
            SELECT log_id, MIN(withdrawn_at) AS withdrawn_at, MIN(reason) AS reason
            FROM `{self._settings.usage_withdrawals_table}`
            WHERE withdrawn_at >= @part_start
              AND withdrawn_at < @part_end
              {by_id}
            GROUP BY log_id
            ORDER BY withdrawn_at DESC, log_id DESC
            {limit}
        """

    def _usage_rows_for_withdrawals_sql(self, fields: tuple[str, ...]) -> str:
        cols = ", ".join(
            f"l.{f}"
            for f in fields
            if f not in {"withdrawn_at", "reason"}
        )
        # Drive the join from the small withdrawn-id array. The usage_log predicate is on
        # occurred_at, its partition column; we scan backward in bounded windows so an old
        # ledger row withdrawn today is still reachable without a single all-partition scan.
        return f"""
            SELECT {cols}
            FROM UNNEST(@log_ids) AS wanted_log_id
            JOIN `{self._settings.usage_log_table}` AS l ON l.log_id = wanted_log_id
            WHERE l.occurred_at >= @part_start
              AND l.occurred_at < @part_end
        """

    def _withdrawal_refs_in_window(
        self,
        *,
        log_id: str | None,
        limit: int,
        part_start: datetime,
        part_end: datetime,
        offset: int = 0,
    ) -> list[dict]:
        from google.cloud import bigquery

        one = log_id is not None
        sql = self._withdrawal_refs_sql(one=one)
        params = [
            bigquery.ScalarQueryParameter("part_start", "TIMESTAMP", part_start.isoformat()),
            bigquery.ScalarQueryParameter("part_end", "TIMESTAMP", part_end.isoformat()),
        ]
        if one:
            params.append(bigquery.ScalarQueryParameter("id", "STRING", log_id))
        else:
            params.append(bigquery.ScalarQueryParameter("scan_limit", "INT64", limit))
            params.append(bigquery.ScalarQueryParameter("scan_offset", "INT64", offset))
        return [dict(r) for r in self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        ).result()]

    def _withdrawal_refs(self, *, log_id: str | None, limit: int) -> list[dict]:
        if limit <= 0:
            return []
        page_size = limit * 5
        out: list[dict] = []
        for start, end in self._partition_windows():
            offset = 0
            while True:
                rows = self._withdrawal_refs_in_window(
                    log_id=log_id,
                    limit=page_size,
                    part_start=start,
                    part_end=end,
                    offset=offset,
                )
                out.extend(rows)
                if log_id is not None and rows:
                    return rows
                if len(out) >= limit:
                    return out[:limit]
                if len(rows) < page_size:
                    break
                offset += page_size
        return out

    def _usage_rows_for_withdrawals(
        self, log_ids: list[str], fields: tuple[str, ...]
    ) -> dict[str, dict]:
        from google.cloud import bigquery

        if not log_ids:
            return {}
        wanted = set(log_ids)
        found: dict[str, dict] = {}
        sql = self._usage_rows_for_withdrawals_sql(fields)
        for start, end in self._partition_windows():
            rows = self._client.query(
                sql,
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ArrayQueryParameter("log_ids", "STRING", sorted(wanted)),
                    bigquery.ScalarQueryParameter("part_start", "TIMESTAMP", start.isoformat()),
                    bigquery.ScalarQueryParameter("part_end", "TIMESTAMP", end.isoformat()),
                ]),
            ).result()
            for row in rows:
                got = dict(row)
                found[got["log_id"]] = got
            wanted -= set(found)
            if not wanted:
                break
        return found

    def _merge_withdrawn_rows(
        self, refs: list[dict], fields: tuple[str, ...]
    ) -> list[dict]:
        usage = self._usage_rows_for_withdrawals([r["log_id"] for r in refs], fields)
        out = []
        for ref in refs:
            row = usage.get(ref["log_id"])
            if row is None:
                continue
            merged = {**row, "withdrawn_at": ref["withdrawn_at"], "reason": ref["reason"]}
            out.append({k: merged.get(k) for k in fields})
        return out

    def list_withdrawn(self, limit: int) -> list[dict]:
        """Steward-only triage view (R6) — newest first, **without ``summary``**.

        The projection is built from :data:`_WITHDRAWN_FIELDS`, the same constant the
        in-memory twin uses, so removing a column from one side cannot leave it in the other
        (a containment-only test could not see that drift).
        """
        if limit <= 0:
            return []
        page_size = limit * 5
        rows: list[dict] = []
        seen: set[str] = set()
        for start, end in self._partition_windows():
            offset = 0
            while True:
                refs = self._withdrawal_refs_in_window(
                    log_id=None,
                    limit=page_size,
                    part_start=start,
                    part_end=end,
                    offset=offset,
                )
                new_refs = [r for r in refs if r["log_id"] not in seen]
                seen.update(r["log_id"] for r in new_refs)
                rows.extend(self._merge_withdrawn_rows(new_refs, _WITHDRAWN_FIELDS))
                if len(rows) >= limit:
                    return rows[:limit]
                if len(refs) < page_size:
                    break
                offset += page_size
        return rows

    def get_withdrawn(self, log_id: str) -> dict | None:
        """Steward-only single row **with** ``summary`` — the investigation step (R6)."""
        refs = self._withdrawal_refs(log_id=log_id, limit=1)
        rows = self._merge_withdrawn_rows(refs, _WITHDRAWN_DETAIL_FIELDS)
        return rows[0] if rows else None
