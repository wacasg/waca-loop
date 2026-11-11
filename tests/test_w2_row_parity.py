"""W2-2/4/5/7 — **双子が同じ行を返す**ことを表明する（docs/TEST_STRATEGY.md §2.4 条件4b）.

SQL を組み立てるところまでの検査では、ゲート句が入っているかは言えても、**見えない行が
返らないか**は言えない。ここでは
`tests/fake_bq.py` の偽クライアント（組み立てた SQL を SQLite で実際に評価する）を挿し、
同じ入力に対して InMemory と BigQuery が**同じものを返す**ことを確かめる。
"""

from __future__ import annotations

import pytest

from fake_bq import FakeBigQueryClient, UnsupportedSql
from waca_loop.grounding.persons import BigQueryPersonStore, InMemoryPersonStore
from waca_loop.models import Person, Viewer


class _Settings:
    def __getattr__(self, name: str) -> str:
        if name.endswith("_table") or name.endswith("_ref"):
            return "p.d." + name.removesuffix("_table").removesuffix("_ref")
        raise AttributeError(name)

    gcp_project = "p"
    embedding_provider = "hashing"


def _person_rows() -> list[dict]:
    return [
        {"person_id": "p_eng", "display_name": "エンジニアの人", "unit_path": ["dept_eng"],
         "consent_internal": True, "consent_external": False, "circles": [], "status": "active"},
        {"person_id": "p_shy", "display_name": "同意していない人", "unit_path": ["dept_eng"],
         "consent_internal": False, "consent_external": False, "circles": [], "status": "active"},
        {"person_id": "p_gone", "display_name": "退職した人", "unit_path": ["dept_eng"],
         "consent_internal": True, "consent_external": False, "circles": [], "status": "frozen"},
    ]


def _in_memory() -> InMemoryPersonStore:
    return InMemoryPersonStore([
        Person(person_id=r["person_id"], display_name=r["display_name"],
               unit_path=list(r["unit_path"]), consent_internal=r["consent_internal"],
               consent_external=r["consent_external"], circles=[], status=r["status"])
        for r in _person_rows()
    ])


def _bigquery() -> BigQueryPersonStore:
    client = FakeBigQueryClient({"p.d.persons": _person_rows()})
    return BigQueryPersonStore(client, _Settings())


MEMBER = Viewer(subject="s", person_id="p_eng", is_org_member=True,
                units=frozenset({"dept_eng"}))
OUTSIDER = Viewer(subject="o", person_id="p_out", is_org_member=False, units=frozenset())


@pytest.mark.parametrize(
    ("viewer", "query"),
    [
        (MEMBER, "人"),          # 見える行がある
        (MEMBER, "存在しない語"),  # 一致なし
        (OUTSIDER, "人"),        # 非メンバー = fail closed
        (None, "人"),            # viewer 無し = fail closed
    ],
)
def test_w2_2_person_search_returns_the_same_people_from_both_twins(viewer, query):
    """見える行・見えない行・存在しない行のすべてで、双子の戻り値が一致すること."""
    im = sorted(p.person_id for p in _in_memory().search(query, 10, viewer))
    bq = sorted(p.person_id for p in _bigquery().search(query, 10, viewer))
    assert im == bq, f"双子の戻り値が違う viewer={viewer} query={query!r}: {im} vs {bq}"


# **この2つの検査は互いを覆わない。** 行の一致（このファイル）は「見えない行が返らない」を
# 見るが、§3.3 の二重ゲートのうち**片方だけ**を外しても、もう片方が残るので行は変わらない
# （BigQuery 側の `search` から SQL ゲートを外しても、in-process の再確認が拾う）。
# SQL 側が欠けたことは `tests/test_twin_conformance.py` の W2-2（句の全文＋行対応）が捕まえる。
# 片方だけでは足りず、両方あって初めて「二重ゲートが二重のまま」だと言える。


def test_w2_2_the_gate_actually_removes_rows_not_just_decorates_the_sql():
    """**行が本当に落ちていること**を、双子それぞれで確かめる.

    同意していない人・退職した人が返っていたら、ゲート句が SQL にあっても意味がない。
    「ACL らしい語がある」と「見えない行が出てこない」は別のこと。
    """
    for store in (_in_memory(), _bigquery()):
        got = {p.person_id for p in store.search("人", 10, MEMBER)}
        assert "p_eng" in got, f"{type(store).__name__}: 見えるはずの本人が返らない"
        assert "p_shy" not in got, f"{type(store).__name__}: 未同意の人が返っている"
        assert "p_gone" not in got, f"{type(store).__name__}: 退職者が返っている"


def _claim_client(map_rows: list[dict], persons: list[dict]) -> FakeBigQueryClient:
    return FakeBigQueryClient({"p.d.identity_map": map_rows, "p.d.persons": persons})


def _person_row(pid: str, status: str) -> dict:
    return {"person_id": pid, "display_name": pid, "unit_path": ["dept_eng"],
            "consent_internal": True, "consent_external": False, "circles": [],
            "status": status}


def _pending(pid: str, email: str) -> dict:
    return {"oauth_sub": f"pending:{pid}:{email}", "person_id": pid, "email": email,
            "claimed_at": None, "claim_source": None, "updated_at": None}


def test_w2_5_a_lone_pending_row_pointing_at_a_retired_person_is_refused():
    """W2-5: 退職者の行は引き当てさせない（アドレス再割当の受け皿にしない）.

    **切り分けに注意**: 「現職＋退職者が同じアドレス」で書くと、退職者判定を外しても
    「指している人が2人」の側で拒否されるので、**このテストは飾りになる**（退職者判定を
    外す変異でも緑のままになる）。他の門が肩代わりしない形にするため、
    保留行は**1つだけ**にして、その人が退職者である場合を置く。
    """
    client = _claim_client([_pending("p_gone", "info@example.org")],
                           [_person_row("p_gone", "frozen")])
    store = BigQueryPersonStore(client, _Settings())

    assert store.claim_pending_registration("info@example.org", "real-sub-999") is None
    assert not any("UPDATE" in s for s in client.sqls), "拒否したのに UPDATE が撃たれている"

    # 双子が同じ判断をすること
    im = InMemoryPersonStore([Person(person_id="p_gone", display_name="p_gone",
                                     unit_path=["dept_eng"], status="frozen")])
    im._identity_map["pending:p_gone:info@example.org"] = "p_gone"   # noqa: SLF001
    assert im.claim_pending_registration("info@example.org", "real-sub-999") is None


def test_w2_5_two_different_people_on_one_address_are_refused():
    """別人が混ざる形も拒否する（共有メールボックス）。上とは**別の門**を見ている."""
    client = _claim_client(
        [_pending("p_a", "info@example.org"), _pending("p_b", "info@example.org")],
        [_person_row("p_a", "active"), _person_row("p_b", "active")],
    )
    store = BigQueryPersonStore(client, _Settings())
    assert store.claim_pending_registration("info@example.org", "real-sub-999") is None
    assert not any("UPDATE" in s for s in client.sqls)


def test_w2_4_the_claim_consumes_only_the_address_that_was_proven():
    """W2-4: 消費するのは**いま証明したアドレスの行だけ**であること.

    **限界を明記する**: `UPDATE ... AND person_id = @pid` の `@pid` 部分は、いまの守り
    （指している人が1人でなければ拒否）が立っている限り**行レベルでは到達不能**であり、
    ここを外しても行の結果は変わらない（変異で確認済み）。つまりこれは多層防御であって、
    その効きは `tests/test_claim_sql_shape.py` の SQL 形状検査が担っている。
    このテストが見ているのは「別アドレスの保留行を巻き添えで消費しない」ほう。
    """
    rows = [
        {"oauth_sub": "pending:p_me:taro@example.org", "person_id": "p_me",
         "email": "taro@example.org", "claimed_at": None, "claim_source": None,
         "updated_at": None},
        {"oauth_sub": "pending:p_me:taro@example.net", "person_id": "p_me",
         "email": "taro@example.net", "claimed_at": None, "claim_source": None,
         "updated_at": None},
    ]
    persons = [{"person_id": "p_me", "display_name": "本人", "unit_path": ["dept_eng"],
                "consent_internal": True, "consent_external": False, "circles": [],
                "status": "active"}]
    client = FakeBigQueryClient({"p.d.identity_map": rows, "p.d.persons": persons})
    store = BigQueryPersonStore(client, _Settings())

    assert store.claim_pending_registration("taro@example.org", "real-sub-1") == "p_me"
    # もう一方のアドレスの行は**保留のまま**であること（消費するのは証明した側だけ）
    left = client.query("SELECT oauth_sub FROM p__d__identity_map", None).result()
    subs = sorted(r["oauth_sub"] for r in left)
    assert "pending:p_me:taro@example.net" in subs, (
        "証明していないアドレスの保留行まで書き換えている"
    )


def test_the_fake_client_refuses_sql_it_cannot_evaluate():
    """未対応の構文を**黙って通さない**こと（未検査を緑にしないため）."""
    client = FakeBigQueryClient({"p.d.persons": _person_rows()})
    with pytest.raises(UnsupportedSql):
        client.query("SELECT * FROM VECTOR_SEARCH(x, 'embedding', y, top_k => 3)", None)


# --- C4: tool_events.recent_by_actor の双子一致（SPEC_coverage §3 C4 / TEST_STRATEGY §2.4）------
#
# 「自分の分だけ」は C4 の要。in-process 側だけ確かめても、本番で行を出すのは SQL のほうなので、
# **BigQuery 側が他人の行を返さないこと**を行で表明する。


def _event_rows() -> list[dict]:
    """Two actors interleaved in one shared trail, plus an event kind C4 must ignore.

    Timestamps are relative to *now*: the BigQuery twin bounds its scan to
    ``RECENT_WINDOW_DAYS``, so fixed dates would quietly age out of the window and turn this
    parity test green-by-emptiness a week after it was written.
    """
    from datetime import UTC, datetime, timedelta

    base = datetime.now(UTC) - timedelta(hours=1)

    def at(seconds: int) -> str:
        return (base + timedelta(seconds=seconds)).isoformat()

    return [
        {"event_id": "e1", "occurred_at": at(0), "tool": "policy_check",
         "actor_id": "p_me", "actor_raw": None, "detail": None, "created_at": "x"},
        {"event_id": "e2", "occurred_at": at(1), "tool": "policy_check",
         "actor_id": "p_other", "actor_raw": None, "detail": None, "created_at": "x"},
        {"event_id": "e3", "occurred_at": at(2), "tool": "usage_recorded",
         "actor_id": "p_me", "actor_raw": None, "detail": None, "created_at": "x"},
        {"event_id": "e4", "occurred_at": at(3), "tool": "scope_declared",
         "actor_id": "p_me", "actor_raw": None, "detail": "external", "created_at": "x"},
        {"event_id": "e5", "occurred_at": at(4), "tool": "policy_check",
         "actor_id": None, "actor_raw": "hash-anon", "detail": None, "created_at": "x"},
    ]


def _event_twins():
    from waca_loop.ledger.tool_events import (
        BigQueryToolEventStore,
        InMemoryToolEventStore,
    )

    mem = InMemoryToolEventStore()
    mem.rows = [dict(r) for r in _event_rows()]
    bq = BigQueryToolEventStore(
        FakeBigQueryClient({"p.d.tool_events": _event_rows()}), _Settings()
    )
    return mem, bq


def _ids(rows: list[dict]) -> list[str]:
    return [r["event_id"] for r in rows]


@pytest.mark.parametrize("store_index", [0, 1], ids=["in_memory", "bigquery"])
def test_recent_by_actor_returns_only_that_actor(store_index):
    store = _event_twins()[store_index]
    rows = store.recent_by_actor(actor_id="p_me", actor_raw=None)
    assert _ids(rows) == ["e1", "e3"]              # 他 actor も scope_declared も出ない
    assert all(r["actor_id"] == "p_me" for r in rows)


@pytest.mark.parametrize("store_index", [0, 1], ids=["in_memory", "bigquery"])
def test_recent_by_actor_does_not_pool_unresolved_callers(store_index):
    """actor_id が NULL の行同士が一括りにならないこと（§5 の resolved/unresolved 分け）。"""
    store = _event_twins()[store_index]
    assert _ids(store.recent_by_actor(actor_id=None, actor_raw="hash-anon")) == ["e5"]
    assert store.recent_by_actor(actor_id=None, actor_raw="hash-someone-else") == []


def test_recent_by_actor_twins_agree_row_for_row():
    mem, bq = _event_twins()
    for actor_id, actor_raw in (("p_me", None), ("p_other", None), (None, "hash-anon"),
                                ("p_nobody", None), (None, None)):
        assert _ids(mem.recent_by_actor(actor_id=actor_id, actor_raw=actor_raw)) == \
               _ids(bq.recent_by_actor(actor_id=actor_id, actor_raw=actor_raw)), (
                   f"twins disagree for {actor_id!r}/{actor_raw!r}")


# --- CV-1e: 双子の窓・上限・同時刻が一致すること -----------------
#
# BigQuery 側にだけ7日窓があると、**本番だけ**が古い行を落とす。その
# 状態は、オフラインのリプレイでは絶対に再現しない種類のずれ（督促が実機とテストで別物に
# なる）。境界・上限・同時刻の3点で、行そのものの一致を表明する。


def _event(event_id: str, ts: str, tool: str = "policy_check") -> dict:
    return {"event_id": event_id, "occurred_at": ts, "tool": tool,
            "actor_id": "p_me", "actor_raw": None, "detail": None, "created_at": "x"}


def _twins_over(rows: list[dict]):
    from waca_loop.ledger.tool_events import (
        BigQueryToolEventStore,
        InMemoryToolEventStore,
    )

    mem = InMemoryToolEventStore()
    mem.rows = [dict(r) for r in rows]
    bq = BigQueryToolEventStore(
        FakeBigQueryClient({"p.d.tool_events": [dict(r) for r in rows]}), _Settings()
    )
    return mem, bq


def test_cv1e_both_twins_drop_rows_older_than_the_window():
    """7日境界: 窓の内側は両方が返し、外側は両方が落とすこと。"""
    from datetime import UTC, datetime, timedelta

    from waca_loop.ledger.tool_events import RECENT_WINDOW_DAYS

    now = datetime.now(UTC)
    rows = [
        _event("inside", (now - timedelta(days=RECENT_WINDOW_DAYS, hours=-2)).isoformat()),
        _event("outside", (now - timedelta(days=RECENT_WINDOW_DAYS, hours=2)).isoformat()),
    ]
    for store in _twins_over(rows):
        got = _ids(store.recent_by_actor(actor_id="p_me", actor_raw=None))
        assert got == ["inside"], f"{type(store).__name__}: 窓の扱いが違う → {got}"


def test_cv1e_both_twins_cap_at_the_same_forty_rows():
    """40件上限: どちらも**新しいほうから**40件で、同じ行を返すこと。"""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC) - timedelta(hours=1)
    rows = [_event(f"e{i:03d}", (now + timedelta(seconds=i)).isoformat()) for i in range(50)]
    mem, bq = _twins_over(rows)
    got_mem = _ids(mem.recent_by_actor(actor_id="p_me", actor_raw=None))
    got_bq = _ids(bq.recent_by_actor(actor_id="p_me", actor_raw=None))
    assert len(got_mem) == 40
    assert got_mem == got_bq
    assert got_mem[-1] == "e049"        # 落とすのは古いほう


def test_cv1e_ties_on_the_same_timestamp_order_the_same_way():
    """同時刻: event_id での決着が双子で一致すること（片方だけ順序が違うと督促が反転する）。"""
    from datetime import UTC, datetime, timedelta

    ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    rows = [_event("b", ts), _event("a", ts), _event("c", ts, tool="usage_recorded")]
    mem, bq = _twins_over(rows)
    assert _ids(mem.recent_by_actor(actor_id="p_me", actor_raw=None)) == \
           _ids(bq.recent_by_actor(actor_id="p_me", actor_raw=None)) == ["a", "b", "c"]


def test_coverage_hint_runs_against_the_production_store(term_store, embedder):
    """C4 の門番は **本番で走るほう** でも効くこと。

    `test_hint_never_carries_another_viewers_coverage` は InMemory ストアを通すので、
    それだけでは「本番では走らないほうのゲート」しか証明しない。ここでは
    `ServerContext` に BigQuery ストア（`fake_bq` で SQL を実評価）を挿して、同じことを
    本番の経路で確かめる。
    """
    from datetime import UTC, datetime, timedelta

    from waca_loop.ledger.tool_events import BigQueryToolEventStore
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore
    from waca_loop.policy.store import InMemoryPolicyStore
    from waca_loop.server import ServerContext

    base = datetime.now(UTC) - timedelta(hours=1)
    rows = []
    # 同僚: 6回確認して1回も記録しない
    for i in range(6):
        rows.append({"event_id": f"o{i}", "occurred_at": (base + timedelta(seconds=i)).isoformat(),
                     "tool": "policy_check", "actor_id": "p_other", "actor_raw": None,
                     "detail": None, "created_at": "x"})
    # 自分: 4ターンすべて記録している
    for i in range(4):
        t = base + timedelta(seconds=100 + i * 2)
        rows.append({"event_id": f"m{i}a", "occurred_at": t.isoformat(), "tool": "policy_check",
                     "actor_id": "p_me", "actor_raw": None, "detail": None, "created_at": "x"})
        rows.append({"event_id": f"m{i}b", "occurred_at": (t + timedelta(seconds=1)).isoformat(),
                     "tool": "usage_recorded", "actor_id": "p_me", "actor_raw": None,
                     "detail": None, "created_at": "x"})

    store = BigQueryToolEventStore(
        FakeBigQueryClient({"p.d.tool_events": rows}), _Settings()
    )
    me = Viewer(subject="s", person_id="p_me", is_org_member=True,
                units=frozenset({"org"}))
    ctx = ServerContext(term_store=None, usage_store=InMemoryUsageLogStore(), embedder=None,
                        viewer=me, policy_store=InMemoryPolicyStore([]),
                        tool_event_store=store)
    assert ctx._coverage_hint() is None, "同僚の6件の未記録が自分のヒントに漏れている"


# --- session_scopes の双子一致--------------------------
#
# 永続化した以上、本番で実際に行を出し入れするのは SQL のほう。「他人の宣言を読まない」を
# in-process 側だけで確かめても、本番の半分を1行も実行していないことになる。


def _scope_rows() -> list[dict]:
    return [
        {"actor_id": "p_me", "actor_raw": None, "scope": "private",
         "declared_at": "2026-08-23T01:00:00+00:00", "updated_at": "2026-08-23T01:00:00+00:00"},
        {"actor_id": "p_other", "actor_raw": None, "scope": "external",
         "declared_at": "2026-08-23T02:00:00+00:00", "updated_at": "2026-08-23T02:00:00+00:00"},
        {"actor_id": None, "actor_raw": "hash-anon", "scope": "private",
         "declared_at": "2026-08-23T03:00:00+00:00", "updated_at": "2026-08-23T03:00:00+00:00"},
    ]


def _scope_twins():
    from datetime import datetime as _dt

    from waca_loop.ledger.session_scopes import (
        BigQuerySessionScopeStore,
        InMemorySessionScopeStore,
    )

    mem = InMemorySessionScopeStore()
    for r in _scope_rows():
        # **同じ時刻で仕込む。** declared_at を渡さないと in-memory 側だけ「いま」になり、
        # 双子が別のデータを持ったまま比較することになる（TTL の起点そのものなので、
        # ここがずれていると失効の検査が何も見ていないのと同じ）。
        mem.upsert(actor_id=r["actor_id"], actor_raw=r["actor_raw"], scope=r["scope"],
                   declared_at=_dt.fromisoformat(r["declared_at"]))
    bq = BigQuerySessionScopeStore(
        FakeBigQueryClient({"p.d.session_scopes": _scope_rows()}), _Settings()
    )
    return mem, bq


@pytest.mark.parametrize("i", [0, 1], ids=["in_memory", "bigquery"])
def test_session_scope_get_returns_only_that_actor(i):
    store = _scope_twins()[i]
    assert store.get(actor_id="p_me", actor_raw=None)[0] == "private"
    assert store.get(actor_id="p_other", actor_raw=None)[0] == "external"
    assert store.get(actor_id="p_nobody", actor_raw=None) is None


@pytest.mark.parametrize("i", [0, 1], ids=["in_memory", "bigquery"])
def test_session_scope_does_not_pool_unresolved_actors(i):
    """actor_id が NULL の行同士が一括りにならないこと（§5 の resolved/unresolved 分け）。"""
    store = _scope_twins()[i]
    assert store.get(actor_id=None, actor_raw="hash-anon")[0] == "private"
    assert store.get(actor_id=None, actor_raw="hash-someone-else") is None
    assert store.get(actor_id=None, actor_raw=None) is None


def test_session_scope_twins_agree():
    mem, bq = _scope_twins()
    for actor_id, actor_raw in (("p_me", None), ("p_other", None), (None, "hash-anon"),
                                ("p_nobody", None), (None, None)):
        a = mem.get(actor_id=actor_id, actor_raw=actor_raw)
        b = bq.get(actor_id=actor_id, actor_raw=actor_raw)
        assert (a is None) == (b is None), f"twins disagree for {actor_id!r}/{actor_raw!r}"
        if a is not None:
            assert a[0] == b[0]


def test_session_scope_twins_agree_when_both_identifiers_are_supplied():
    """`ledger_actor` は片方しか返さないが、ストアは公開面。両方渡されても双子がずれないこと。

    SQL 側は actor_id を優先する（`@actor_id IS NOT NULL` が先）。in-process 側が組を
    そのまま鍵にすると、両方渡された瞬間だけ二者が別の行を見る — 起きたときに最も気づき
    にくい形の食い違いになる。
    """
    mem, bq = _scope_twins()
    a = mem.get(actor_id="p_me", actor_raw="hash-anon")
    b = bq.get(actor_id="p_me", actor_raw="hash-anon")
    assert a is not None and b is not None
    assert a[0] == b[0] == "private"        # actor_id 側の行。actor_raw に引きずられない


def test_session_scope_upsert_actually_runs_on_the_bigquery_side():
    """upsert の SQL 側を**実行する**。組み立てた SQL を見るだけでは、DELETE+INSERT が
    本番で壊れても気づけない。"""
    from waca_loop.ledger.session_scopes import BigQuerySessionScopeStore

    client = FakeBigQueryClient({"p.d.session_scopes": _scope_rows()})
    bq = BigQuerySessionScopeStore(client, _Settings())
    bq.upsert(actor_id="p_new", actor_raw=None, scope="private")
    # 追記のみ＝DML を投げない。DML を投げる実装に戻ったらここで気づく
    assert not any("DELETE" in s.upper() for s in client.sqls)
    assert bq.get(actor_id="p_new", actor_raw=None)[0] == "private"

    # 行が両方の識別子を持たないこと。持つと、未解決の呼び出し元が actor_raw で
    # 解決済みの人の宣言を引ける。
    bq.upsert(actor_id="p_both", actor_raw="hash-x", scope="private")
    assert bq.get(actor_id=None, actor_raw="hash-x") is None
    assert bq.get(actor_id="p_both", actor_raw=None)[0] == "private"


def test_session_scope_declared_at_is_tz_aware_on_both_twins():
    """`declared_at` は TTL の入力そのもの。naive だと `scope_expired` が黙って False を返し、
    **失効しなくなる**（guard の fail-closed 分岐に落ちる）ので、型まで表明する。"""
    from datetime import UTC, datetime

    from waca_loop.guard import scope_expired

    for store in _scope_twins():
        scope, declared_at = store.get(actor_id="p_me", actor_raw=None)
        assert scope == "private"
        assert isinstance(declared_at, datetime), f"{type(store).__name__}: {declared_at!r}"
        assert declared_at.tzinfo is not None, f"{type(store).__name__} は naive を返している"
        # **値まで見る。** 型だけ見ると、読めない時刻を「いま」に落とす fallback が働いても
        # 緑のままになり、TTL の起点が黙って現在時刻にすり替わる。
        assert declared_at == datetime(2026, 8, 23, 1, 0, tzinfo=UTC), (
            f"{type(store).__name__} が保存した時刻を返していない: {declared_at!r}"
        )
        scope_expired(declared_at, ttl_hours=12)     # 例外なく評価できること
