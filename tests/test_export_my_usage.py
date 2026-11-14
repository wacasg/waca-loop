"""export_my_usage — 本人が自分の台帳記録だけを書き出す（§2.2③ 要約のみ・§3.3）.

守る性質:

* 返るのは**呼び出した本人の記録だけ**。他人の記録がストアにあっても、それが新しくても、
  本人の記録が0件でも、混ざらない。
* 本人を確認できない／組織の利用者でない viewer には空を返し、理由を添える。代わりの
  何か（actor_raw 等）へ落ちない。
* 取り下げた記録は ``withdrawn: true`` で、要約を出さない。
* ``limit`` は ``_clamp_k`` で締まる（break-glass も効かない）。
* CSV のヘッダと行数が rows と一致する。
* ツールに人を指定する引数が**存在しない**。
"""

from __future__ import annotations

import csv
import inspect
import io
from datetime import UTC, datetime, timedelta

import pytest

from fake_bq import FakeBigQueryClient
from waca_loop.auth.identity import claim_preregistration, resolve_viewer
from waca_loop.auth.oauth import Identity
from waca_loop.embeddings import HashingEmbedder
from waca_loop.grounding.persons import BigQueryPersonStore, InMemoryPersonStore, pending_sub
from waca_loop.grounding.terms import InMemoryTermStore
from waca_loop.ledger.tool_events import InMemoryToolEventStore
from waca_loop.ledger.usage_log import BigQueryUsageLogStore, InMemoryUsageLogStore
from waca_loop.models import Person, UsageRecord, UseCategory, Viewer
from waca_loop.server import _K_CAPS, ServerContext

A = Viewer(subject="sa", person_id="p_a", is_org_member=True, units=frozenset({"org"}))
B = Viewer(subject="sb", person_id="p_b", is_org_member=True, units=frozenset({"org"}))
EXPORT_ADMIN = Viewer(
    subject="sx", person_id="p_a", is_org_member=True, units=frozenset({"org", "export_admin"})
)

_FORBIDDEN_KEYS = {"prompt", "raw_text", "transcript", "verbatim", "actor_raw", "request_id"}


def _ctx(viewer, store, trail=None, **kw):
    emb = HashingEmbedder()
    return ServerContext(
        term_store=InMemoryTermStore([], emb),
        usage_store=store,
        embedder=emb,
        viewer=viewer,
        person_store=InMemoryPersonStore(persons=[], identity_map={}),
        tool_event_store=trail,
        **kw,
    )


def _rec(store, actor_id, summary, *, at=None, actor_raw=None):
    return store.record(
        UsageRecord(
            actor_id=actor_id,
            actor_raw=actor_raw,
            client="claude_code",
            use_category=UseCategory.L1,
            data_kinds=["internal"],
            summary=summary,
            policy_row_ids=["R-1"],
        ),
        occurred_at=at or datetime.now(UTC),
        dedupe=False,
    )


def test_caller_sees_only_own_rows():
    store = InMemoryUsageLogStore()
    now = datetime.now(UTC)
    a1 = _rec(store, "p_a", "Aの要約1", at=now - timedelta(days=2))
    a2 = _rec(store, "p_a", "Aの要約2", at=now - timedelta(days=1))
    # B's rows are newer than A's — they must still never show up.
    for i in range(5):
        _rec(store, "p_b", f"Bの要約{i}", at=now - timedelta(minutes=i))
    _rec(store, None, "未解決の要約", actor_raw="hash-x")

    out = _ctx(A, store).export_my_usage()
    assert out["actor"] == "p_a"
    assert out["count"] == 2
    assert [r["log_id"] for r in out["rows"]] == [a2, a1]       # newest first
    assert all("B" not in (r.get("summary") or "") for r in out["rows"])
    assert "Bの要約" not in out["csv"] and "未解決" not in out["csv"]
    assert "本人の記録のみ" in out["notice"]
    for r in out["rows"]:
        assert not (_FORBIDDEN_KEYS | {"actor_id"}) & set(r)


def test_caller_with_no_rows_gets_nothing_even_when_others_have_rows():
    store = InMemoryUsageLogStore()
    for i in range(3):
        _rec(store, "p_b", f"Bの要約{i}")
    out = _ctx(A, store).export_my_usage()
    assert out["count"] == 0 and out["rows"] == []
    assert out["csv"].strip().count("\n") == 0          # header only


@pytest.mark.parametrize(
    "viewer",
    [
        Viewer.anonymous("hash-x"),
        Viewer(subject="sz", person_id=None, is_org_member=True, units=frozenset({"org"})),
        Viewer(subject="so", person_id="p_out", is_org_member=False),
    ],
)
def test_unresolved_or_non_member_gets_empty_with_reason(viewer):
    store = InMemoryUsageLogStore()
    _rec(store, "p_b", "Bの要約")
    _rec(store, None, "未解決の要約", actor_raw="hash-x")
    _rec(store, "p_out", "外部の要約")
    trail = InMemoryToolEventStore()
    out = _ctx(viewer, store, trail).export_my_usage()
    assert out["count"] == 0 and out["rows"] == []
    assert out["reason"]
    assert "要約" not in out["csv"].split("\n", 1)[1]
    assert any(r["tool"] == "usage_exported" and r["detail"].startswith("refused=")
               for r in trail.rows)


def test_withdrawn_row_is_marked_and_its_summary_omitted():
    store = InMemoryUsageLogStore()
    kept = _rec(store, "p_a", "残す要約")
    gone = _rec(store, "p_a", "取り下げた私的な要約")
    assert store.withdraw(gone, actor_id="p_a", actor_raw=None, reason="private")

    out = _ctx(A, store).export_my_usage()
    by_id = {r["log_id"]: r for r in out["rows"]}
    assert by_id[kept]["withdrawn"] is False and by_id[kept]["summary"] == "残す要約"
    assert by_id[gone]["withdrawn"] is True
    assert "summary" not in by_id[gone]
    assert "取り下げた私的な要約" not in out["csv"]


def test_limit_is_clamped_and_break_glass_does_not_apply():
    store = InMemoryUsageLogStore()
    cap = _K_CAPS["export_my_usage"]
    now = datetime.now(UTC)
    for i in range(cap + 5):
        _rec(store, "p_a", f"s{i}", at=now - timedelta(seconds=i))
    out = _ctx(A, store).export_my_usage(limit=cap * 100)
    assert out["count"] == cap and out["limit"] == cap
    # An export admin stating no reason (the tool has no reason argument) and even with an
    # audit sink wired cannot exceed it either — export_my_usage is in _NO_BREAK_GLASS.
    ctx = _ctx(EXPORT_ADMIN, store, InMemoryToolEventStore(),
               export_admin_units=frozenset({"export_admin"}))
    assert ctx._clamp_k(cap * 100, "export_my_usage", reason="audit") == cap
    assert _ctx(A, store).export_my_usage(limit=3)["count"] == 3


def test_days_window_is_applied_and_bounded():
    store = InMemoryUsageLogStore()
    now = datetime.now(UTC)
    recent = _rec(store, "p_a", "最近", at=now - timedelta(days=3))
    _rec(store, "p_a", "古い", at=now - timedelta(days=40))
    out = _ctx(A, store).export_my_usage(days=7)
    assert [r["log_id"] for r in out["rows"]] == [recent]
    assert _ctx(A, store).export_my_usage(days=10_000)["days"] == 365
    assert _ctx(A, store).export_my_usage(days=0)["days"] == 1


def test_csv_header_and_row_count_match_rows():
    store = InMemoryUsageLogStore()
    for i in range(4):
        _rec(store, "p_a", f"要約,カンマ\"引用\"{i}")
    gone = _rec(store, "p_a", "取り下げ")
    store.withdraw(gone, actor_id="p_a", actor_raw=None, reason="mistake")
    out = _ctx(A, store).export_my_usage()
    parsed = list(csv.reader(io.StringIO(out["csv"])))
    header, body = parsed[0], parsed[1:]
    assert header == [
        "log_id", "occurred_at", "client", "use_category", "data_kinds", "summary",
        "policy_row_ids", "withdrawn",
    ]
    assert len(body) == out["count"] == len(out["rows"]) == 5
    assert [row[0] for row in body] == [r["log_id"] for r in out["rows"]]
    w = next(row for row in body if row[0] == gone)
    assert w[header.index("summary")] == "" and w[header.index("withdrawn")] == "true"


def test_the_export_is_trailed_with_counts_only():
    store = InMemoryUsageLogStore()
    _rec(store, "p_a", "個人的な要約テキスト")
    trail = InMemoryToolEventStore()
    _ctx(A, store, trail).export_my_usage(days=7, limit=10)
    ev = [r for r in trail.rows if r["tool"] == "usage_exported"]
    assert len(ev) == 1
    assert ev[0]["detail"] == "days=7:k=10:n=1"
    assert ev[0]["actor_id"] == "p_a"


def test_no_person_or_actor_parameter_exists():
    params = set(inspect.signature(ServerContext.export_my_usage).parameters)
    assert params == {"self", "days", "limit"}


@pytest.mark.asyncio
async def test_bound_mcp_tool_has_no_person_or_actor_parameter():
    pytest.importorskip("mcp")
    from waca_loop.server import build_mcp

    tools = await build_mcp(_ctx(A, InMemoryUsageLogStore())).list_tools()
    tool = next(t for t in tools if t.name == "export_my_usage")
    props = set((tool.inputSchema or {}).get("properties", {}))
    assert props == {"days", "limit"}


# --- 双子の BigQuery 側 — 実際に SQL を評価して同じ行が返ること --------------------


class _Settings:
    usage_log_table = "p.d.usage_log"
    usage_withdrawals_table = "p.d.usage_withdrawals"
    usage_log_retention_days = 730


def _bq_store() -> tuple[BigQueryUsageLogStore, FakeBigQueryClient]:
    seed_w = {
        "withdrawal_id": "seed", "log_id": "seed", "actor_id": "p_seed", "actor_raw": None,
        "reason": "private", "withdrawn_at": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    client = FakeBigQueryClient({_Settings.usage_withdrawals_table: [seed_w]})
    return BigQueryUsageLogStore(client, _Settings()), client


def _fill(store):
    now = datetime.now(UTC)
    ids = {
        "a_old": _rec(store, "p_a", "A古い", at=now - timedelta(days=40)),
        "a1": _rec(store, "p_a", "A1", at=now - timedelta(days=2)),
        "a2": _rec(store, "p_a", "A2", at=now - timedelta(days=1)),
        "b": _rec(store, "p_b", "B新しい", at=now - timedelta(minutes=1)),
    }
    assert store.withdraw(ids["a1"], actor_id="p_a", actor_raw=None, reason="private")
    return ids


@pytest.mark.parametrize("make", ["memory", "bigquery"])
def test_twins_return_the_same_rows(make):
    if make == "memory":
        store, client = InMemoryUsageLogStore(), None
    else:
        store, client = _bq_store()
    ids = _fill(store)
    got = store.rows_for_actor("p_a", days=30, limit=10)
    assert [r["log_id"] for r in got] == [ids["a2"], ids["a1"]]
    assert {r["actor_id"] for r in got} == {"p_a"}
    assert got[0]["summary"] == "A2" and got[0]["withdrawn"] is False
    assert got[1]["summary"] is None and got[1]["withdrawn"] is True
    assert set(got[0]) == {
        "log_id", "occurred_at", "actor_id", "client", "use_category", "data_kinds",
        "summary", "policy_row_ids", "withdrawn",
    }
    assert got[0]["data_kinds"] == ["internal"] and got[0]["policy_row_ids"] == ["R-1"]
    assert store.rows_for_actor("p_a", days=30, limit=1)[0]["log_id"] == ids["a2"]
    assert store.rows_for_actor("", days=30, limit=10) == []
    assert store.rows_for_actor("p_nobody", days=30, limit=10) == []
    if client is not None:
        export_sqls = [s for s in client.sqls if "AS withdrawn" in s]
        assert export_sqls
        for s in export_sqls:
            assert "@actor_id" in s and "p_a" not in s       # bound, never formatted in
            assert "l.occurred_at >= @since" in s            # partition-bounded
            assert "usage_withdrawals" in s


def test_csv_neutralises_formula_cells():
    from waca_loop.server import ServerContext

    rows = [{"log_id": "x", "summary": "=HYPERLINK(\"http://evil\")", "data_kinds": [],
             "policy_row_ids": [], "withdrawn": False}]
    out = ServerContext._usage_csv(rows)
    assert "'=HYPERLINK" in out


# --- 結び付け時刻の床（SPEC §4.1） ----------------------------------------------------
#
# person_id をメールアドレスから決める運用（受講生の登録など）では、台帳は登録解除後も
# 残る。同じアドレスが別人に渡って登録されると同じ person_id になるので、書き出しは
# 「いまのログインがその person_id に結び付いた時刻」以降の記録だけを返す。


def _mapped_ctx(subject, persons, store, trail=None):
    viewer = resolve_viewer(Identity(subject=subject), persons)
    assert viewer.identity_mapped and viewer.person_id
    emb = HashingEmbedder()
    return ServerContext(
        term_store=InMemoryTermStore([], emb),
        usage_store=store,
        embedder=emb,
        viewer=viewer,
        person_store=persons,
        tool_event_store=trail,
    )


def _claim(persons, email, subject, *, at):
    ident = Identity(subject=subject, email=email, email_verified=True)
    pid = claim_preregistration(ident, persons, frozenset())
    assert pid
    persons._claims[subject]["claimed_at"] = at    # 時刻だけ決め打ちにする（意図的に内部へ）
    return pid


def _persons_with_pending(email="taro@example.com", pid="p_member_abc"):
    return InMemoryPersonStore(
        persons=[Person(person_id=pid, display_name="受講生", unit_path=["org"])],
        identity_map={pending_sub(email, pid): pid},
    )


def test_rows_before_the_bind_time_are_excluded():
    store = InMemoryUsageLogStore()
    persons = _persons_with_pending()
    now = datetime.now(UTC)
    bind = now - timedelta(days=5)
    pid = _claim(persons, "taro@example.com", "sub-new", at=bind)
    before = _rec(store, pid, "結び付け前", at=bind - timedelta(seconds=1))
    at_bind = _rec(store, pid, "結び付けちょうど", at=bind)
    after = _rec(store, pid, "結び付け後", at=now - timedelta(days=1))

    out = _mapped_ctx("sub-new", persons, store).export_my_usage(days=30)
    got = [r["log_id"] for r in out["rows"]]
    assert got == [after, at_bind]
    assert before not in got and "結び付け前" not in out["csv"]
    assert datetime.fromisoformat(out["since"]) == bind
    assert "結び付けた時点より前の記録は含みません" in out["notice"]


def test_day_window_still_wins_when_it_is_later_than_the_bind_time():
    store = InMemoryUsageLogStore()
    persons = _persons_with_pending()
    now = datetime.now(UTC)
    pid = _claim(persons, "taro@example.com", "sub-new", at=now - timedelta(days=100))
    _rec(store, pid, "窓の外", at=now - timedelta(days=10))
    inside = _rec(store, pid, "窓の内", at=now - timedelta(days=2))
    out = _mapped_ctx("sub-new", persons, store).export_my_usage(days=7)
    assert [r["log_id"] for r in out["rows"]] == [inside]
    since = datetime.fromisoformat(out["since"])
    assert now - timedelta(days=7, seconds=5) < since <= now - timedelta(days=7) + timedelta(
        seconds=5
    )


def test_recycled_address_does_not_hand_over_the_previous_holders_rows():
    store = InMemoryUsageLogStore()
    email, pid = "shared@example.com", "p_member_abc"
    persons = _persons_with_pending(email, pid)
    now = datetime.now(UTC)
    _claim(persons, email, "sub-old", at=now - timedelta(days=60))
    old_rows = [_rec(store, pid, f"前の持ち主{i}", at=now - timedelta(days=50 - i))
                for i in range(3)]
    # 前の持ち主の登録解除（台帳は残す）→ 同じアドレスで別人が再登録
    del persons._identity_map["sub-old"]
    persons._identity_map[pending_sub(email, pid)] = pid
    _claim(persons, email, "sub-new", at=now - timedelta(days=3))
    mine = _rec(store, pid, "新しい持ち主", at=now - timedelta(days=1))

    out = _mapped_ctx("sub-new", persons, store).export_my_usage(days=365)
    assert [r["log_id"] for r in out["rows"]] == [mine]
    assert not set(old_rows) & {r["log_id"] for r in out["rows"]}
    assert "前の持ち主" not in out["csv"]


def test_unknown_bind_time_in_a_mapped_context_is_refused():
    store = InMemoryUsageLogStore()
    # 管理者が sub を直接入れた行（claimed_at なし）— 結び付け時刻は不明
    persons = InMemoryPersonStore(
        persons=[Person(person_id="p_a", display_name="A", unit_path=["org"])],
        identity_map={"sub-admin": "p_a"},
    )
    _rec(store, "p_a", "Aの要約")
    trail = InMemoryToolEventStore()
    out = _mapped_ctx("sub-admin", persons, store, trail).export_my_usage()
    assert out["count"] == 0 and out["rows"] == []
    assert "スチュワード" in out["reason"]
    assert "since" not in out
    assert "Aの要約" not in out["csv"]
    assert any(r["tool"] == "usage_exported" and r["detail"] == "refused=bind_unknown"
               for r in trail.rows)


def test_bind_lookup_failure_or_missing_store_is_refused():
    store = InMemoryUsageLogStore()
    persons = _persons_with_pending()
    pid = _claim(persons, "taro@example.com", "sub-new", at=datetime.now(UTC))
    _rec(store, pid, "要約")
    ctx = _mapped_ctx("sub-new", persons, store)

    class _Broken:
        def bound_at(self, subject, person_id):
            raise RuntimeError("bq down")

    for ps in (None, object(), _Broken()):
        ctx.person_store = ps
        out = ctx.export_my_usage()
        assert out["count"] == 0 and "スチュワード" in out["reason"]


def test_bound_at_is_keyed_by_the_current_sub_not_the_person():
    persons = _persons_with_pending()
    now = datetime.now(UTC)
    pid = _claim(persons, "taro@example.com", "sub-new", at=now)
    assert persons.bound_at("sub-new", pid) == now
    assert persons.bound_at("sub-other", pid) is None
    assert persons.bound_at("sub-new", "p_someone_else") is None
    assert persons.bound_at("", pid) is None


def test_in_process_viewer_without_identity_map_has_no_floor():
    # テスト・オフライン開発の viewer は identity_mapped=False — 日数の窓だけが効く。
    assert A.identity_mapped is False
    store = InMemoryUsageLogStore()
    old = _rec(store, "p_a", "古め", at=datetime.now(UTC) - timedelta(days=20))
    out = _ctx(A, store).export_my_usage(days=30)
    assert [r["log_id"] for r in out["rows"]] == [old]


@pytest.mark.parametrize("make", ["memory", "bigquery"])
def test_twins_apply_the_bind_floor_identically(make):
    if make == "memory":
        store, client = InMemoryUsageLogStore(), None
    else:
        store, client = _bq_store()
    ids = _fill(store)
    now = datetime.now(UTC)
    floor = now - timedelta(days=1, hours=12)        # a1(2日前) より後、a2(1日前) より前
    got = store.rows_for_actor("p_a", days=30, limit=10, now=now, not_before=floor)
    assert [r["log_id"] for r in got] == [ids["a2"]]
    # 床が窓より古ければ窓が効く
    got = store.rows_for_actor(
        "p_a", days=30, limit=10, now=now, not_before=now - timedelta(days=90)
    )
    assert [r["log_id"] for r in got] == [ids["a2"], ids["a1"]]
    if client is not None:
        for s in [s for s in client.sqls if "AS withdrawn" in s]:
            assert "l.occurred_at >= @since" in s


def test_bigquery_person_store_bound_at_is_bound_and_fails_closed():
    class _S:
        identity_map_table = "p.g.oauth_identity_map"

    t = "2026-11-01T00:00:00+00:00"
    rows = [
        {"oauth_sub": "s1", "person_id": "p1", "email": None, "created_at": t,
         "updated_at": t, "claimed_at": t, "claim_source": "preregistration"},
        {"oauth_sub": "s2", "person_id": "p2", "email": None, "created_at": t,
         "updated_at": t, "claimed_at": None, "claim_source": None},
        {"oauth_sub": "s1", "person_id": "p_other", "email": None, "created_at": t,
         "updated_at": t, "claimed_at": "2020-01-01T00:00:00+00:00", "claim_source": "admin"},
    ]
    client = FakeBigQueryClient({_S.identity_map_table: rows})
    ps = BigQueryPersonStore(client, _S())
    assert ps.bound_at("s1", "p1") == datetime(2026, 11, 1, tzinfo=UTC)
    assert ps.bound_at("s2", "p2") is None             # NULL → unknown
    assert ps.bound_at("s1", "p2") is None             # sub と person の組でしか引かない
    assert ps.bound_at("", "p1") is None
    sql = next(s for s in client.sqls if "claimed_at" in s)
    assert "@sub" in sql and "@pid" in sql and "s1" not in sql
