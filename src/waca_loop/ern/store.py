"""`ern_assertions` / `ern_audit` のストア対（INV-3・SPEC_phase3 §1）.

各バックエンドに InMemory と BigQuery が**同一サーフェス**で対存在する。テストは
InMemory 側で完全オフラインに回り（INV-2）、BigQuery 側は SQL を組み立てるところまでを
同じ名前で提供する。

**E7 append-only**: どちらのストアにも `update` / `delete` / `set_*` を置かない。
同じ検索を2回すれば2行になる。件数は導出値（集計）であって、行の書き換えでは表さない。
`tests/test_e07_append_only.py` がメソッド一覧を走査して表明する。

**§4f**: `ern_audit` を**読み出す MCP ツールは作らない**。ここに `list` / `search` を
置かないのはそのため — 置いた瞬間、「誰が誰を探したか」を返す道具が1つできる。
ライフサイクル（§5 の purge）だけが、この表を走査してよい唯一の経路である。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings


def _utcnow() -> datetime:
    return datetime.now(UTC)


def new_ern_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class ErnAssertion:
    """`<DS_KNOWLEDGE>.ern_assertions` の1行（SPEC_phase3 §2.1）.

    **rank / score / rating / order を持たない**（E3）。順位や総合点をここに残すと、
    ERN が評価装置でないという前提がデータ側から崩れる。
    """

    assertion_id: str
    person_id: str
    topic: str
    embedding: list[float] = field(default_factory=list)
    source_candidate_id: str | None = None
    status: str = "declared"      # declared | derived | frozen


@dataclass
class ErnAuditRow:
    """`<DS_FEEDBACK>.ern_audit` の1行（SPEC_phase3 §2.3 ③）.

    「いつ・誰が・何を根拠に候補になったか」。**問い合わせ本文は持たない**（INV-1/E8）—
    `topic` は正規化済みの領域ラベルであって、利用者が書いた文ではない。
    """

    audit_id: str
    occurred_at: datetime
    actor_id: str | None
    actor_raw: str | None
    topic: str | None
    candidate_ids: list[str] = field(default_factory=list)
    evidence_types: list[str] = field(default_factory=list)


# --- assertions ------------------------------------------------------------------------------

class InMemoryErnAssertionStore:
    """宣言/導出プロファイルの InMemory 側（Gate2 の in-process 側・§3.3）.

    読み取りは **viewer 必須**。既定値を持たせない — 呼び忘れた新しい経路でゲートが
    黙って無効になる（`feedback_for` が同じ理由で必須にしている）。

    `ern_assertions` の行は「誰がどの領域を宣言したか」で、**それ自体が名簿の断片**である。
    「あとで `person_visible_to` に掛けるから安全」では §3.3 の片側しか無く、この store を
    直接呼ぶ経路が1つ増えた瞬間に、オプトアウトした人の person_id が外へ出る。
    """

    def __init__(
        self,
        assertions: list[ErnAssertion] | None = None,
        persons: dict[str, object] | None = None,
    ) -> None:
        self._rows: list[ErnAssertion] = list(assertions or [])
        self._persons = dict(persons or {})

    def add(self, assertion: ErnAssertion) -> None:
        self._rows.append(assertion)

    def _disclosable(self, person_id: str, viewer) -> bool:
        from waca_loop.retrieval.acl import person_visible_to

        person = self._persons.get(person_id)
        if person is None:
            # 名簿に無い person_id は出さない（fail-closed）。宣言だけが残った行は、
            # 在籍も同意も確かめられない。
            return False
        return person_visible_to(person, viewer)

    def topics_by_person(self, viewer) -> dict[str, list[str]]:
        """person_id → 宣言された領域。frozen は返さない（§5: 退職者は候補にしない）."""
        out: dict[str, list[str]] = {}
        for row in self._rows:
            if row.status == "frozen" or not self._disclosable(row.person_id, viewer):
                continue
            out.setdefault(row.person_id, []).append(row.topic)
        return out

    def persons_for_topic(self, topic: str, viewer) -> list[str]:
        """その領域を宣言している person_id（リコール）. 昇順・重複なし."""
        t = (topic or "").strip().lower()
        return sorted({
            r.person_id for r in self._rows
            if r.status != "frozen"
            and r.topic.strip().lower() == t
            and self._disclosable(r.person_id, viewer)
        })


class BigQueryErnAssertionStore:
    """同上の BigQuery 側（INV-3 の対）."""

    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    @property
    def _table(self) -> str:
        return self._settings.ern_assertions_table

    def add(self, assertion: ErnAssertion) -> None:
        from google.cloud import bigquery

        now = _utcnow()
        sql = f"""
            INSERT INTO `{self._table}`
              (assertion_id, person_id, topic, embedding, source_candidate_id,
               status, created_at, updated_at)
            VALUES (@assertion_id, @person_id, @topic, @embedding, @source_candidate_id,
                    @status, @created_at, @updated_at)
        """  # noqa: S608 — テーブル参照は settings 由来、値はすべてバインド
        params = [
            bigquery.ScalarQueryParameter("assertion_id", "STRING", assertion.assertion_id),
            bigquery.ScalarQueryParameter("person_id", "STRING", assertion.person_id),
            bigquery.ScalarQueryParameter("topic", "STRING", assertion.topic),
            bigquery.ArrayQueryParameter("embedding", "FLOAT64", list(assertion.embedding or [])),
            bigquery.ScalarQueryParameter(
                "source_candidate_id", "STRING", assertion.source_candidate_id
            ),
            bigquery.ScalarQueryParameter("status", "STRING", assertion.status),
            bigquery.ScalarQueryParameter("created_at", "TIMESTAMP", now),
            bigquery.ScalarQueryParameter("updated_at", "TIMESTAMP", now),
        ]
        self._client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params)
        ).result()

    def _gate(self, viewer) -> tuple[str, dict]:
        """名簿ゲートを **SQL の側**にも書く（§4c・E4(b)）.

        `person_where_clause` を `persons` への相関副問い合わせに包む。EXISTS の中で
        `p.person_id = a.person_id` と対応させてあるのが要 — 対応が無いと
        「見える人が1人でもいれば全部通る」形になり、ゲートの体をなさない
        （`test_twin_conformance` の `_correlates_rows` はまさにそこを見ている）。
        """
        from waca_loop.retrieval.acl import person_where_clause

        gate_sql, params = person_where_clause(viewer, alias="p")
        exists = (
            f"EXISTS (SELECT 1 FROM `{self._settings.persons_table}` AS p "
            f"WHERE p.person_id = a.person_id AND {gate_sql})"
        )
        return exists, params

    def topics_by_person(self, viewer) -> dict[str, list[str]]:
        from google.cloud import bigquery

        from waca_loop.bq import scalar_params

        gate, params = self._gate(viewer)
        sql = (
            f"SELECT a.person_id, a.topic FROM `{self._table}` AS a "  # noqa: S608
            f"WHERE a.status != 'frozen' AND {gate}"
        )
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=scalar_params(params)),
        )
        out: dict[str, list[str]] = {}
        for row in job.result():
            out.setdefault(row["person_id"], []).append(row["topic"])
        return out

    def persons_for_topic(self, topic: str, viewer) -> list[str]:
        from google.cloud import bigquery

        from waca_loop.bq import scalar_params

        gate, params = self._gate(viewer)
        sql = (
            f"SELECT DISTINCT a.person_id FROM `{self._table}` AS a "  # noqa: S608
            f"WHERE a.status != 'frozen' AND LOWER(TRIM(a.topic)) = @topic AND {gate} "
            "ORDER BY a.person_id"
        )
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter(
                        "topic", "STRING", (topic or "").strip().lower()
                    ),
                    *scalar_params(params),
                ]
            ),
        )
        return [r["person_id"] for r in job.result()]


# --- audit（append-only・読み出しツールを作らない） --------------------------------------------

class InMemoryErnAuditStore:
    """監査証跡の InMemory 側。`rows` はテストが覗くためのもので、ツールの経路ではない."""

    def __init__(self) -> None:
        self.rows: list[ErnAuditRow] = []

    def record(
        self,
        *,
        actor_id: str | None,
        actor_raw: str | None,
        topic: str | None,
        candidate_ids: list[str],
        evidence_types: list[str],
        occurred_at: datetime | None = None,
    ) -> str:
        row = ErnAuditRow(
            audit_id=new_ern_id("erna"),
            occurred_at=occurred_at or _utcnow(),
            actor_id=actor_id,
            actor_raw=actor_raw,
            topic=topic,
            candidate_ids=list(candidate_ids),
            evidence_types=list(evidence_types),
        )
        self.rows.append(row)
        return row.audit_id


class BigQueryErnAuditStore:
    """同上の BigQuery 側（INV-3 の対）."""

    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    @property
    def _table(self) -> str:
        return self._settings.ern_audit_table

    def record(
        self,
        *,
        actor_id: str | None,
        actor_raw: str | None,
        topic: str | None,
        candidate_ids: list[str],
        evidence_types: list[str],
        occurred_at: datetime | None = None,
    ) -> str:
        now = occurred_at or _utcnow()
        audit_id = new_ern_id("erna")
        row = {
            "audit_id": audit_id,
            "occurred_at": now.isoformat(),
            "actor_id": actor_id,
            "actor_raw": actor_raw,
            "topic": topic,
            "candidate_ids": list(candidate_ids),
            "evidence_types": list(evidence_types),
            "created_at": now.isoformat(),
        }
        errors = self._client.insert_rows_json(self._table, [row])
        if errors:
            raise RuntimeError(f"ern_audit insert failed: {errors}")
        return audit_id
