"""OAuth DCR client persistence for the hosted AS (docs/SETUP_gcp.md §8).

Claude.ai self-registers via Dynamic Client Registration and then *stores its client_id
forever*. If the AS kept registered clients only in process-memory, a Cloud Run restart would
forget them — after which even "reconnect" would fail (the saved client_id would no longer
exist). Registered clients are the one piece of AS state that must
outlive the process, so they follow the project's store pattern: an InMemory + BigQuery pair
with the same surface (``get`` / ``put``).

The stored record is the SDK's ``OAuthClientInformationFull`` serialized as JSON. It contains
client metadata and (when the client registered with a secret-bearing auth method) the
``client_secret`` — a machine credential, not PII. It lives in the access-restricted
grounding dataset alongside ``oauth_identity_map`` and must never be logged (same handling
as tokens).

BigQuery writes are insert-only (streaming rows can't be updated in place); ``get`` reads the
newest row per ``client_id`` and keeps a process-local read-through cache so /token calls
don't query BigQuery on every request.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mcp.shared.auth import OAuthClientInformationFull

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings


def client_to_row(client_info: OAuthClientInformationFull, created_at: datetime) -> dict:
    """Serialize a registered client to an ``oauth_clients`` row."""
    return {
        "client_id": client_info.client_id,
        "client_json": client_info.model_dump_json(),
        "created_at": created_at.isoformat(),
    }


def row_to_client(row: dict) -> OAuthClientInformationFull:
    return OAuthClientInformationFull.model_validate_json(row["client_json"])


class InMemoryOAuthClientStore:
    """In-memory client registry (tests / offline). Same surface as the BigQuery store."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def get(self, client_id: str) -> OAuthClientInformationFull | None:
        for row in reversed(self.rows):  # newest registration wins, like the BQ ORDER BY
            if row["client_id"] == client_id:
                return row_to_client(row)
        return None

    def put(self, client_info: OAuthClientInformationFull) -> None:
        self.rows.append(client_to_row(client_info, datetime.now(UTC)))


class BigQueryOAuthClientStore:
    """Production client registry backed by BigQuery streaming inserts (insert-only)."""

    _MISS_TTL = 60.0  # seconds a "no such client" answer is cached (unauthenticated /token
    #                   calls with bogus client_ids must not each cost a BigQuery query)

    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._cache: dict[str, OAuthClientInformationFull] = {}
        self._miss_until: dict[str, float] = {}  # client_id -> monotonic deadline

    def get(self, client_id: str) -> OAuthClientInformationFull | None:
        import time

        cached = self._cache.get(client_id)
        if cached is not None:
            return cached
        if self._miss_until.get(client_id, 0.0) > time.monotonic():
            return None
        from google.cloud import bigquery

        sql = f"""
            SELECT client_json
            FROM `{self._settings.oauth_clients_table}`
            WHERE client_id = @cid
            ORDER BY created_at DESC
            LIMIT 1
        """
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("cid", "STRING", client_id)]
            ),
        )
        rows = list(job.result())
        if not rows:
            import time

            self._miss_until[client_id] = time.monotonic() + self._MISS_TTL
            return None
        client_info = row_to_client({"client_json": rows[0]["client_json"]})
        self._cache[client_id] = client_info
        return client_info

    def put(self, client_info: OAuthClientInformationFull) -> None:
        row = client_to_row(client_info, datetime.now(UTC))
        errors = self._client.insert_rows_json(self._settings.oauth_clients_table, [row])
        if errors:
            raise RuntimeError(f"oauth_clients insert failed: {errors}")
        if client_info.client_id is not None:
            self._cache[client_info.client_id] = client_info
            self._miss_until.pop(client_info.client_id, None)
