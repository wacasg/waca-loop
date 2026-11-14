"""Person stores (§2.2 ②) + identity resolution (§5).

People are PII, kept in a separate table. The OAuth ``sub`` → ``person_id`` mapping lives
in ``oauth_identity_map`` (also PII-separated). Unresolved subjects are recorded as a
hashed ``actor_raw`` for later 名寄せ (§5).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict

from waca_loop.models import Person, Viewer

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings


# Pre-registration sentinel (§5). `oauth_identity_map.oauth_sub` is NOT NULL, so a person who
# has never logged in is seeded as `pending:<person_id>:<email>` — a value Google can never
# issue (real subs are digits). On that person's first login the row is rebound to their sub.
#
# This is what lets the org register staff from the org sheet's email column ahead of time,
# instead of the "first login always fails, an admin reads the sub out of Cloud Logging, then
# you try again" dance. The email is only ever trusted when the ID token says it is verified.
PENDING_SUB_PREFIX = "pending:"

# How a row came to carry a real `sub` (§5 監査痕跡). The claim REBINDS the pending row in
# place, so after it fires the row looks exactly like one an admin registered by hand. Without
# recording the route, "この人はシート由来で自動的に束ねられた" and "管理者が sub を直接入れた"
# cannot be told apart afterwards — which is the difference between an automated binding and a
# human decision, the thing an audit of an access grant most wants to see.
CLAIM_SOURCE_PREREGISTRATION = "preregistration"  # sheet row, bound on the person's own login
CLAIM_SOURCE_ADMIN = "admin"                      # a human wrote the sub in directly


class ClaimTrace(TypedDict):
    """The audit trail of one claim — contractual against the two BigQuery columns (§5)."""

    claimed_at: datetime
    claim_source: str


def pending_sub(email: str, person_id: str) -> str:
    """The sentinel `oauth_sub` for a pre-registered, not-yet-logged-in person.

    Keyed by person AND address, never by address alone: `person_id` is derived from the
    display name, so a rename in the sheet (or two people sharing a mailbox) would otherwise
    produce two rows with the SAME sentinel — the claim would rebind both, and
    `resolve_person_id` (LIMIT 1, no ORDER BY) would then return either person at random,
    handing one of them the other's unit scope.
    """
    return f"{PENDING_SUB_PREFIX}{person_id}:{normalize_email(email)}"


def _fold_fullwidth(s: str) -> str:
    """全角 ASCII (U+FF01–FF5E) と全角空白だけを半角に畳む。

    NFKC ではなく、この狭い範囲に限るのは意図的である。NFKC は ``test①@example.org`` を
    ``test1@example.org`` に、``ofﬁce@example.org``(ﬁ 合字) を ``office@example.org`` に、
    ``K``(U+212A) を ``k`` に畳む。事前登録は「アドレス1件 → person_id 1件」で引き当てる
    ので、装飾された別人の行が実在する誰かのアドレスに畳まれると、**本人がログインしただけで他人の
    person_id と可視範囲に束ねられる**（監査証跡には正規の事前登録として残る）。
    IME が混ぜるのは全角 ASCII であって合字や Kelvin 記号ではないから、直したい範囲だけ
    直す。
    """
    out = []
    for ch in s:
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:
            out.append(chr(cp - 0xFEE0))
        elif cp == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def normalize_email(email: str) -> str:
    """Case/whitespace-insensitive key for email matching (addresses are not case-sensitive).

    全角 ASCII を先に畳むのは、組織シートが日本語 IME で打たれるからである。
    「goro.ito＠example.com」(全角 ＠, U+FF20) は目には正しく見えて人のレビューを
    通り抜けるのに、Google が `email` クレームで返す ASCII のアドレスとは決して一致しない。
    本人は何も一致しないキーで事前登録され、原因を指す手がかりも無いままログインできない。
    シートの見た目が「誰が入れるか」を決める状態にしない。

    畳む範囲を全角 ASCII に限る理由は :func:`_fold_fullwidth` を参照。(§5)
    """
    return _fold_fullwidth(email or "").strip().lower()


def hash_subject(subject: str) -> str:
    """Stable, non-reversible actor_raw for an unmapped OAuth subject (§5)."""
    return "sub_" + hashlib.sha256(subject.encode("utf-8")).hexdigest()[:32]


class InMemoryPersonStore:
    """In-memory person store + identity map (tests / offline)."""

    def __init__(
        self,
        persons: list[Person] | None = None,
        identity_map: dict[str, str] | None = None,
    ) -> None:
        self._persons = {p.person_id: p for p in (persons or [])}
        self._identity_map = dict(identity_map or {})  # oauth_sub -> person_id
        # 監査痕跡 (§5): the claim OVERWRITES the pending row, so without this the row
        # afterwards is indistinguishable from one an admin typed in directly. Mirrors the
        # BigQuery twin's `claimed_at` / `claim_source` columns.
        #
        # Private on purpose: the only public read of it is :meth:`bound_at`, which both
        # twins implement (the BigQuery one SELECTs `claimed_at`). Tests reach in
        # deliberately for `claim_source`; production reads that with `bq query`.
        self._claims: dict[str, ClaimTrace] = {}  # oauth_sub -> trace

    def resolve_person_id(self, subject: str) -> str | None:
        return self._identity_map.get(subject)

    def bound_at(self, subject: str, person_id: str) -> datetime | None:
        """When this ``sub`` was bound to ``person_id`` (``claimed_at``), else ``None`` (§5).

        Keyed by the *current* sub AND the person, never by the person alone: another login
        that once held the same person_id (a recycled address — SPEC_export_my_usage §4.1) must not
        lend its earlier bind time. ``None`` means "unknown" — a row seeded directly into the
        map (admin-bound, or bound before ``claimed_at`` existed) — and callers fail closed.
        """
        if not subject or self._identity_map.get(subject) != person_id:
            return None
        trace = self._claims.get(subject)
        return trace["claimed_at"] if trace else None

    def _is_active(self, person_id: str) -> bool:
        """退職者(frozen)への引き当てを拒む (§5, store twin と同じ規則).

        アドレスが再割当されると、旧担当者の保留行がそのまま新任者の引き当て先になり、
        **他人のユニット権限をそのまま引き継ぐ**。運用(シートから行を消す)だけに頼らず、
        コード側にも受け皿を置かない。
        """
        person = self._persons.get(person_id)
        return person is None or person.status == "active"

    def claim_pending_registration(self, email: str, subject: str) -> str | None:
        """Bind a pre-registered person to their real ``sub`` on first login (§5).

        Returns the person_id when exactly one pending row existed (and rebinds it), else
        None. Caller MUST have verified the email with the identity provider first — see
        :attr:`waca_loop.auth.oauth.Identity.email_verified`.
        """
        suffix = ":" + normalize_email(email)
        keys = [
            k for k, _ in self._identity_map.items()
            if k.startswith(PENDING_SUB_PREFIX) and k.endswith(suffix)
        ]
        if not keys:
            return None   # 事前登録なし
        # 判定は「行がちょうど1つ」ではなく「**指している人がちょうど1人**」。
        # 一人が組織の二つのドメイン(@example.org / @example.net)を行き来するのは実際に
        # あることで、行数で切ると本人が自分のもう一方のアドレスで締め出される。危険なのは
        # 行が複数あることではなく、**別人を指す行が混ざっていること**なので、そこだけを見る。
        person_ids = {self._identity_map[k] for k in keys}
        if len(person_ids) != 1:
            return None   # 別人が混ざっている → どちらか決められないので拒否
        person_id = next(iter(person_ids))
        if not self._is_active(person_id):
            return None   # 退職者の行は引き当てさせない（アドレス再割当の受け皿にしない）
        # 消費するのは **いま本人が証明したアドレスの行だけ**。
        #
        # 「その人の保留行を残らず」消すと、別アドレスの拒否を解除してしまう。
        # 共有メールボックスの行は「2人を指す行が並んでいる」ことで拒否されており、拒否は
        # その行**でできている**。片方を消すと拒否が消え、そのメールボックスを持つ誰かが
        # 残った1人に束ねられる — しかも監査痕跡は preregistration(正規)と記録する。
        #
        # 本人の別アドレスの行を残しても穴にはならない。その行を引き当てるには**そのアドレスを
        # 検証済みで提示する**必要があり、それができるのは本人だけだから。
        for k in [k for k in list(self._identity_map)
                  if k.startswith(PENDING_SUB_PREFIX) and k.endswith(suffix)]:
            del self._identity_map[k]
        self._identity_map[subject] = person_id
        self._claims[subject] = ClaimTrace(
            claimed_at=datetime.now(UTC),
            claim_source=CLAIM_SOURCE_PREREGISTRATION,
        )
        return person_id

    def get(self, person_id: str) -> Person | None:
        return self._persons.get(person_id)

    def get_many(self, person_ids: list[str], viewer: Viewer) -> dict[str, Person]:
        """複数人をまとめて引く。**viewer 必須・ゲート済み**（§3.3 の in-process 側）.

        `get` と別物にしてあるのは、`get` が本人解決（`resolve_person_id` の後始末）にも
        使われていて、そこはゲートを掛ける対象ではないから。名前を**候補として並べる**
        用途は別の関数にし、そちらは viewer を省略できないようにする。
        """
        from waca_loop.retrieval.acl import person_visible_to

        out: dict[str, Person] = {}
        for pid in person_ids:
            person = self._persons.get(pid)
            if person is not None and person_visible_to(person, viewer):
                out[pid] = person
        return out

    def search(self, query: str, limit: int, viewer: Viewer | None = None) -> list[Person]:
        """Name search, ACL-gated (§3.3). ``viewer=None`` returns nothing — fail closed."""
        from waca_loop.retrieval.acl import person_visible_to

        q = (query or "").strip().lower()
        if not q or viewer is None:
            return []
        hits = [
            p
            for p in self._persons.values()
            if q in p.display_name.lower() and person_visible_to(p, viewer)
        ]
        return hits[:limit]

    def active_actor_ids(self) -> set[str]:
        return {p.person_id for p in self._persons.values() if p.status == "active"}

    def roster_names(self) -> tuple[str, ...]:
        """在籍者の表示名一覧 — **名前を出さないための検査**にだけ使う (§5)。

        閲覧者を取らない。名簿を見せるための口ではなく、書こうとしている文に人の名前が
        残っていないかを機械が見るためのもので、呼び出し側は名前そのものを外へ出さない
        (氏名は anonymize の検出器では捕まらない)。
        """
        return tuple(p.display_name for p in self._persons.values() if p.status == "active")


def _row_to_person(row: Any) -> Person:
    return Person(
        person_id=row["person_id"],
        display_name=row["display_name"],
        unit_path=list(row["unit_path"]) if row.get("unit_path") else [],
        consent_internal=bool(row["consent_internal"]),
        consent_external=bool(row["consent_external"]),
        circles=list(row["circles"]) if row.get("circles") else [],
        status=row.get("status", "active"),
    )


class BigQueryPersonStore:
    """Production person store backed by BigQuery (§2.2 ②, §5)."""

    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def resolve_person_id(self, subject: str) -> str | None:
        from google.cloud import bigquery

        sql = f"""
            SELECT person_id
            FROM `{self._settings.identity_map_table}`
            WHERE oauth_sub = @sub
            LIMIT 1
        """
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("sub", "STRING", subject)]
            ),
        )
        rows = list(job.result())
        return rows[0]["person_id"] if rows else None

    def bound_at(self, subject: str, person_id: str) -> datetime | None:
        """BigQuery twin of :meth:`InMemoryPersonStore.bound_at` — ``claimed_at`` of this sub.

        Both the sub and the person_id are bound parameters in the WHERE. If several rows
        match (duplicate seed rows) and ANY of them has no ``claimed_at``, the answer is
        ``None`` (unknown → the caller refuses); otherwise the LATEST time, which is the
        stricter floor. Fail closed either way.
        """
        if not subject or not person_id:
            return None
        from google.cloud import bigquery

        sql = f"""
            SELECT claimed_at
            FROM `{self._settings.identity_map_table}`
            WHERE oauth_sub = @sub AND person_id = @pid
        """
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("sub", "STRING", subject),
                    bigquery.ScalarQueryParameter("pid", "STRING", person_id),
                ]
            ),
        )
        times = [r["claimed_at"] for r in job.result()]
        if not times or any(t is None for t in times):
            return None
        parsed = [
            datetime.fromisoformat(t) if isinstance(t, str) else t for t in times
        ]
        return max(t if t.tzinfo else t.replace(tzinfo=UTC) for t in parsed)

    def claim_pending_registration(self, email: str, subject: str) -> str | None:
        """Bind a pre-registered person to their real ``sub`` on first login (§5, store twin).

        The UPDATE is the claim: it matches only a row still carrying the `pending:` sentinel,
        so it can fire at most once per pre-registration — a second login (or a second person
        who somehow presented the same address) finds nothing to claim. Caller MUST have
        verified the email with the identity provider first.
        """
        from google.cloud import bigquery

        table = self._settings.identity_map_table
        # STARTS_WITH/ENDS_WITH, NOT LIKE. The address goes into the pattern verbatim, and
        # `_` — ordinary in a local part (taro_yamada@) — is a LIKE wildcard matching ANY
        # single character. `pending:%:taro_yamada@example.org` would therefore also match
        # `pending:p_other:taro.yamada@example.org`, and the COUNT(*)=1 guard would not help:
        # it counts the SAME widened pattern, so a single colliding row would read as a clean
        # hit and the UPDATE would rebind ANOTHER person's person_id and unit scope onto this
        # sub — which the audit trail would then stamp `preregistration`, i.e. legitimate.
        # The in-memory twin uses an exact `endswith`, so no offline test could catch such a
        # drift. §5
        match_sql = "STARTS_WITH(oauth_sub, @prefix) AND ENDS_WITH(oauth_sub, @suffix)"
        match_params = [
            bigquery.ScalarQueryParameter("prefix", "STRING", PENDING_SUB_PREFIX),
            bigquery.ScalarQueryParameter("suffix", "STRING", ":" + normalize_email(email)),
        ]
        # 判定は「行がちょうど1つ」ではなく「**指している人がちょうど1人**」(store twin)。
        # 一人が組織の二つのドメインを行き来するのは実際にあることで、行数で切ると本人が
        # 自分のもう一方のアドレスで締め出される。危険なのは行が複数あることではなく、
        # **別人を指す行が混ざっていること**(改名・共有メールボックス)なので、そこだけを見る。
        # persons と突き合わせ、退職者(status != 'active')の行は候補から外す — アドレス
        # 再割当で新任者が旧担当者のユニット権限を引き継ぐ受け皿にしない。
        # **数えてから、退職者を弾く。逆順にしない。** JOIN の WHERE に
        # `p.status = 'active'` を置くと、退職者の行が DISTINCT の前に消え、
        # 「現職A＋退職者B が同じ共有メールボックスを指す」状態が **1人に見えて通って
        # しまう**。InMemory 双子は全行で数えてから active を見るので拒否する。
        # BigQuery 側だけが受理する形にしない — この関数はオフラインテストが届かないので、
        # 順序を双子に揃える。
        # status 不明(persons に行が無い)は双子の `_is_active` と同じく active 扱い。
        count_job = self._client.query(
            f"""SELECT DISTINCT m.person_id, COALESCE(p.status, 'active') AS status
                FROM `{table}` AS m
                LEFT JOIN `{self._settings.persons_table}` AS p USING (person_id)
                WHERE {match_sql.replace('oauth_sub', 'm.oauth_sub')}""",
            job_config=bigquery.QueryJobConfig(query_parameters=list(match_params)),
        )
        rows = list(count_job.result())
        if len({r["person_id"] for r in rows}) != 1:
            return None   # 別人が混ざっている → どちらか決められないので拒否
        if any(r["status"] != "active" for r in rows):
            return None   # 退職者の行は引き当てさせない（アドレス再割当の受け皿にしない）
        person_id = rows[0]["person_id"]
        # 消費するのは **いま本人が証明したアドレスの、その人の行だけ** (store twin)。
        # その人の保留行を残らず消すと、共有メールボックスの「2人を指す行が並ぶことで
        # 成立している拒否」を片側から崩してしまう。
        # `person_id = @pid` は必須 — 守り(上の SELECT)が1人に絞った意味は、書く側が同じ
        # 人に限って初めて残る。@pid を bind しながら WHERE で使わないと、
        # そのアドレスの保留行を**人を問わず全部**書き換えてしまう。
        job = self._client.query(
            f"""
            UPDATE `{table}`
            SET oauth_sub = @sub, email = @email, updated_at = CURRENT_TIMESTAMP(),
                claimed_at = CURRENT_TIMESTAMP(), claim_source = @claim_source
            WHERE {match_sql} AND person_id = @pid
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("sub", "STRING", subject),
                    bigquery.ScalarQueryParameter("email", "STRING", normalize_email(email)),
                    bigquery.ScalarQueryParameter(
                        "claim_source", "STRING", CLAIM_SOURCE_PREREGISTRATION
                    ),
                    bigquery.ScalarQueryParameter("pid", "STRING", person_id),
                    *match_params,
                ]
            ),
        )
        job.result()
        # `>= 1`。同じ人・同じアドレスの重複シード行が両方 UPDATE されることがあり、
        # `!= 1` で弾くと **引き当ては済んでいるのにログインだけ拒否**され、
        # 「未登録」の警告が出た本人が実は登録済み、という分かりにくい形になる。
        if (job.num_dml_affected_rows or 0) < 1:
            return None
        return self.resolve_person_id(subject)

    def get(self, person_id: str) -> Person | None:
        from google.cloud import bigquery

        sql = f"SELECT * FROM `{self._settings.persons_table}` WHERE person_id = @pid LIMIT 1"
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("pid", "STRING", person_id)]
            ),
        )
        rows = list(job.result())
        return _row_to_person(rows[0]) if rows else None

    def get_many(self, person_ids: list[str], viewer: Viewer) -> dict[str, Person]:
        """複数人をまとめて引く。**ゲートは SQL 側にも掛ける**（§3.3 の二重ゲート）.

        `get` は1行を id で引くだけでゲートを持たない（本人解決にも使うため）。名前を
        **候補として並べる**経路がそれを流用すると、オプトアウトした同僚の行が
        `display_name` / `unit_path` / `consent_internal` ごと BigQuery から出てきて、
        絞り込みは in-process だけになる — §3.3 の前提（見えない行はそもそも DB から
        出ない）が崩れる。

        ついでに N 件の単発クエリが1本になる。ERN は問いの著者を全部引くので、
        件数は `_K_CAPS["find_expert"]` ではなく問いプールの著者数で決まる。
        """
        from google.cloud import bigquery

        from waca_loop.bq import scalar_params
        from waca_loop.retrieval.acl import person_visible_to, person_where_clause

        ids = [i for i in dict.fromkeys(person_ids) if i]
        if not ids:
            return {}
        gate_sql, params = person_where_clause(viewer, alias="p")
        sql = (
            f"SELECT p.* FROM `{self._settings.persons_table}` AS p "  # noqa: S608
            f"WHERE p.person_id IN UNNEST(@person_ids) AND {gate_sql}"
        )
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("person_ids", "STRING", ids),
                    *scalar_params(params),
                ]
            ),
        )
        # Gate 2: 取ってから同じ規則で再確認する（片方の論理が抜けても漏れない）。
        out: dict[str, Person] = {}
        for row in job.result():
            person = _row_to_person(row)
            if person_visible_to(person, viewer):
                out[person.person_id] = person
        return out

    def search(self, query: str, limit: int, viewer: Viewer | None = None) -> list[Person]:
        """Name search, ACL-gated in SQL then re-checked in-process (§3.3 二重で効かせる).

        ``viewer=None`` returns nothing — the roster is PII and never leaves BigQuery
        un-gated. Gate 1 is ``person_where_clause``; Gate 2 is ``person_visible_to`` below.
        """
        from google.cloud import bigquery

        from waca_loop.bq import scalar_params
        from waca_loop.retrieval.acl import person_visible_to, person_where_clause

        # STRPOS(name, '') returns 1, so an empty/blank query would match every row the ACL
        # lets through — a roster dump of the viewer's own units. The InMemory twin already
        # refuses blanks; keep the surfaces identical (store-pattern parity).
        q = (query or "").strip().lower()
        if viewer is None or not q:
            return []
        gate_sql, gate_params = person_where_clause(viewer, alias="p")
        sql = f"""
            SELECT p.* FROM `{self._settings.persons_table}` AS p
            WHERE {gate_sql}
              AND STRPOS(LOWER(p.display_name), @q) > 0
            LIMIT @lim
        """
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("q", "STRING", q),
                    bigquery.ScalarQueryParameter("lim", "INT64", limit),
                    *scalar_params(gate_params),
                ]
            ),
        )
        persons = [_row_to_person(row) for row in job.result()]
        return [p for p in persons if person_visible_to(p, viewer)]  # Gate 2

    def active_actor_ids(self) -> set[str]:
        sql = f"SELECT person_id FROM `{self._settings.persons_table}` WHERE status = 'active'"
        return {row["person_id"] for row in self._client.query(sql).result()}

    def roster_names(self) -> tuple[str, ...]:
        """在籍者の表示名一覧（store twin）— 名前を出さないための検査専用 (§5)。

        **viewer を取らないのは正しい。** これは可視性を広げる読み口ではなく「書こうと
        している文に人の名前が残っていないか」を否定判定するためのもので、名簿を**絞ると
        検査が甘くなる**（漏れる側に倒れる）。だから consent や circles で絞らない。
        呼び出し側は真偽に潰してから使い、氏名を戻り値・例外・ログ・台帳へ出さないこと
        — この非対称が崩れた時点で、ここは ACL を素通りする第二の読み口になる。
        """
        sql = (
            f"SELECT display_name FROM `{self._settings.persons_table}` "
            "WHERE status = 'active' AND display_name IS NOT NULL"
        )
        return tuple(row["display_name"] for row in self._client.query(sql).result())
