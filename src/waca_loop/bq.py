"""Thin BigQuery client wrapper (lazy import so pure-logic modules stay dep-free)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings


def make_client(settings: Settings, credentials: Any | None = None) -> bigquery.Client:
    """Construct a BigQuery client for the configured project (§0)."""
    from google.cloud import bigquery

    return bigquery.Client(
        project=settings.gcp_project,
        location=settings.bq_location,
        credentials=credentials,
    )


def scalar_params(params: dict) -> list:
    """Map a ``{name: value}`` dict to BigQuery query parameters.

    Lists become ARRAY params; bools/ints/strings become scalars. Used to bind the
    ACL clause params (:func:`waca_loop.retrieval.acl.acl_where_clause`).
    """
    from google.cloud import bigquery

    out = []
    for name, value in params.items():
        if isinstance(value, bool):
            out.append(bigquery.ScalarQueryParameter(name, "BOOL", value))
        elif isinstance(value, int):
            out.append(bigquery.ScalarQueryParameter(name, "INT64", value))
        elif isinstance(value, float):
            out.append(bigquery.ScalarQueryParameter(name, "FLOAT64", value))
        elif isinstance(value, (list, tuple, set)):
            out.append(bigquery.ArrayQueryParameter(name, "STRING", list(value)))
        else:
            out.append(bigquery.ScalarQueryParameter(name, "STRING", value))
    return out
