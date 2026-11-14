"""結び付け時刻の床を、本人向けの台帳経路すべてにかける（SPEC_export_my_usage §4.1）.

person_id をメールアドレスから決める運用（受講生の登録など）では、台帳は登録解除後も
残る。アドレスが別人に渡って再登録されると同じ person_id になるので、「本人の記録」は
いまのログイン（sub）がその person_id に結び付いた時刻以降のものに限る。

対象: ``export_my_usage``（tests/test_export_my_usage.py）に加えて
``start_policy_workbench``（要約と log_id を返す）と ``request_log_deletion``（取り下げ）。
床は ``ServerContext._owner_floor()`` で1回だけ求め、三者で共有する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from fake_bq import FakeBigQueryClient
from waca_loop.auth.identity import claim_preregistration, resolve_viewer
from waca_loop.auth.oauth import Identity
from waca_loop.embeddings import HashingEmbedder
from waca_loop.grounding.persons import InMemoryPersonStore, pending_sub
from waca_loop.grounding.terms import InMemoryTermStore
from waca_loop.ledger.tool_events import InMemoryToolEventStore
from waca_loop.ledger.usage_log import BigQueryUsageLogStore, InMemoryUsageLogStore
from waca_loop.models import Person, UsageRecord, UseCategory, Viewer
from waca_loop.server import ServerContext

EMAIL, PID = "shared@example.com", "p_member_abc"


def _rec(store, actor_id, summary, *, at, actor_raw=None):
    return store.record(
        UsageRecord(
            actor_id=actor_id, actor_raw=actor_raw, client="claude_code",
            use_category=UseCategory.L1, data_kinds=["internal"], summary=summary,
            policy_row_ids=["R-1"],
        ),
        occurred_at=at,
        dedupe=False,
    )


def _claim(persons, subject, *, at):
    ident = Identity(subject=subject, email=EMAIL, email_verified=True)
    assert claim_preregistration(ident, persons, frozenset()) == PID
    persons._claims[subject]["claimed_at"] = at     # 時刻だけ決め打ち（意図的に内部へ）


def _persons():
    return InMemoryPersonStore(
        persons=[Person(person_id=PID, display_name="受講生", unit_path=["org"])],
        identity_map={pending_sub(EMAIL, PID): PID},
    )


def _recycled(store):
    """前の持ち主が記録 → 登録解除（台帳は残す）→ 同じアドレスで別人が再登録."""
    persons = _persons()
    now = datetime.now(UTC)
    _claim(persons, "sub-old", at=now - timedelta(days=60))
    old = [_rec(store, PID, f"前の持ち主の要約{i}", at=now - timedelta(days=10 + i))
           for i in range(3)]
    del persons._identity_map["sub-old"]
    persons._identity_map[pending_sub(EMAIL, PID)] = PID
    bind = now - timedelta(days=3)
    _claim(persons, "sub-new", at=bind)
    mine = _rec(store, PID, "新しい持ち主の要約", at=now - timedelta(days=1))
    return persons, old, mine, bind


def _ctx(subject, persons, store, trail=None):
    viewer = resolve_viewer(Identity(subject=subject), persons)
    assert viewer.identity_mapped and viewer.person_id == PID
    emb = HashingEmbedder()
    return ServerContext(
        term_store=InMemoryTermStore([], emb), usage_store=store, embedder=emb,
        viewer=viewer, person_store=persons, tool_event_store=trail,
    )


def _admin_bound(store):
    """管理者が sub を直接入れた行（claimed_at なし）— 結び付け時刻は不明."""
    persons = InMemoryPersonStore(
        persons=[Person(person_id=PID, display_name="受講生", unit_path=["org"])],
        identity_map={"sub-admin": PID},
    )
    row = _rec(store, PID, "誰のものか分からない要約", at=datetime.now(UTC))
    return persons, row


# --- _owner_floor ------------------------------------------------------------------


def test_owner_floor_is_computed_once_and_shared():
    store = InMemoryUsageLogStore()
    persons, _, _, bind = _recycled(store)
    calls = []

    class _Counting:
        def bound_at(self, subject, person_id):
            calls.append((subject, person_id))
            return persons.bound_at(subject, person_id)

    ctx = _ctx("sub-new", persons, store)
    ctx.person_store = _Counting()
    assert ctx._owner_floor() == (bind, None)
    ctx.export_my_usage()
    ctx.start_policy_workbench()
    assert calls == [("sub-new", PID)]


def test_owner_floor_for_in_process_viewer_is_none():
    emb = HashingEmbedder()
    ctx = ServerContext(
        term_store=InMemoryTermStore([], emb), usage_store=InMemoryUsageLogStore(),
        embedder=emb,
        viewer=Viewer(subject="s", person_id="p", is_org_member=True, units=frozenset({"org"})),
    )
    assert ctx._owner_floor() == (None, None)


# --- 1. start_policy_workbench -------------------------------------------------------


def test_workbench_does_not_surface_the_previous_holders_summaries():
    store = InMemoryUsageLogStore()
    persons, old, mine, _ = _recycled(store)
    out = _ctx("sub-new", persons, store).start_policy_workbench()
    assert out["source_log_ids"] == [mine]
    assert not set(old) & set(out["source_log_ids"])
    assert "前の持ち主" not in str(out)
    assert "logs_withheld_reason" not in out


def test_workbench_with_unknown_bind_time_gets_no_logs_but_still_answers():
    store = InMemoryUsageLogStore()
    persons, row = _admin_bound(store)
    out = _ctx("sub-admin", persons, store).start_policy_workbench()
    assert out["source_log_ids"] == []
    assert row not in str(out) and "誰のものか分からない" not in str(out)
    assert "スチュワード" in out["logs_withheld_reason"]
    assert out["session_id"].startswith("workbench:")       # the tool itself did not fail


def test_in_memory_recent_summaries_applies_not_before():
    store = InMemoryUsageLogStore()
    now = datetime.now(UTC)
    v = Viewer(subject="s", person_id=PID, is_org_member=True, units=frozenset({"org"}))
    old = _rec(store, PID, "古い", at=now - timedelta(days=5))
    new = _rec(store, PID, "新しい", at=now - timedelta(days=1))
    assert [s.log_id for s in store.recent_summaries_for_actor(v, PID)] == [new, old]
    got = store.recent_summaries_for_actor(v, PID, not_before=now - timedelta(days=2))
    assert [s.log_id for s in got] == [new]


class _RecordingClient:
    """BigQuery の recent_summaries_for_actor を、SQL とパラメタの形で確かめる偽物.

    返す行は floor より古い行を**わざと含める** — WHERE が壊れても in-process の
    二段目で落ちることを見るため。
    """

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query(self, sql, job_config=None):
        params = {p.name: p.value for p in job_config.query_parameters}
        self.calls.append((sql, params))
        return SimpleNamespace(result=lambda: list(self.rows))


def test_bigquery_recent_summaries_binds_the_floor_and_rechecks_in_process():
    now = datetime.now(UTC)
    floor = now - timedelta(days=2)
    base = {"actor_id": PID, "owner_unit": None, "use_category": "l1",
            "data_kinds": ["internal"], "summary": "s", "policy_row_ids": []}
    client = _RecordingClient([
        {**base, "log_id": "new", "occurred_at": now - timedelta(days=1)},
        {**base, "log_id": "old", "occurred_at": now - timedelta(days=5)},
    ])
    settings = SimpleNamespace(
        usage_log_table="p.d.usage_log", usage_withdrawals_table="p.d.usage_withdrawals",
        persons_table="p.g.persons", usage_log_retention_days=730,
    )
    store = BigQueryUsageLogStore(client, settings)
    v = Viewer(subject="s", person_id=PID, is_org_member=True, units=frozenset({"org"}))
    got = store.recent_summaries_for_actor(v, PID, limit=10, not_before=floor)
    assert [s.log_id for s in got] == ["new"]
    sql, params = client.calls[-1]
    assert "l.occurred_at >= @since" in sql and params["since"] == floor
    assert floor.isoformat() not in sql
    # 床なしなら述語も足さない（既存の呼び出しは変わらない）
    store.recent_summaries_for_actor(v, PID, limit=10)
    assert "@since" not in client.calls[-1][0]


# --- 2. request_log_deletion -----------------------------------------------------------


def test_recycled_address_cannot_withdraw_the_previous_holders_row():
    store = InMemoryUsageLogStore()
    persons, old, mine, _ = _recycled(store)
    trail = InMemoryToolEventStore()
    ctx = _ctx("sub-new", persons, store, trail)
    with pytest.raises(ValueError, match="not found"):        # same answer as "not yours"
        ctx.request_log_deletion(old[0], "private")
    assert old[0] not in store._withdrawn_ids()
    assert ctx.request_log_deletion(mine, "private")["withdrawn"] is True
    assert [r["tool"] for r in trail.rows if r["tool"].startswith("usage_withdraw")] == [
        "usage_withdraw_refused", "usage_withdrawn",
    ]


def test_withdrawal_with_unknown_bind_time_is_refused_with_reason():
    store = InMemoryUsageLogStore()
    persons, row = _admin_bound(store)
    trail = InMemoryToolEventStore()
    with pytest.raises(ValueError, match="スチュワード"):
        _ctx("sub-admin", persons, store, trail).request_log_deletion(row, "private")
    assert row not in store._withdrawn_ids()
    assert any(r["tool"] == "usage_withdraw_refused" for r in trail.rows)


def test_own_pre_binding_rows_by_subject_hash_stay_withdrawable():
    """sub 自身のハッシュで残った行（結び付け前の本人の行）は床の対象外 — 同じ Google sub."""
    from waca_loop.grounding.persons import hash_subject

    store = InMemoryUsageLogStore()
    persons = _persons()
    now = datetime.now(UTC)
    pre = _rec(store, None, "結び付け前の本人", at=now - timedelta(days=9),
               actor_raw=hash_subject("sub-new"))
    _claim(persons, "sub-new", at=now - timedelta(days=3))
    assert _ctx("sub-new", persons, store).request_log_deletion(pre, "private")["withdrawn"]


def _bq_usage():
    seed_w = {
        "withdrawal_id": "seed", "log_id": "seed", "actor_id": "p_seed", "actor_raw": None,
        "reason": "private", "withdrawn_at": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    settings = SimpleNamespace(
        usage_log_table="p.d.usage_log", usage_withdrawals_table="p.d.usage_withdrawals",
        usage_log_retention_days=730,
    )
    client = FakeBigQueryClient({settings.usage_withdrawals_table: [seed_w]})
    return BigQueryUsageLogStore(client, settings), client


@pytest.mark.parametrize("make", ["memory", "bigquery"])
def test_twins_apply_the_withdrawal_floor_identically(make):
    store, client = (InMemoryUsageLogStore(), None) if make == "memory" else _bq_usage()
    now = datetime.now(UTC)
    floor = now - timedelta(days=3)
    old = _rec(store, PID, "古い", at=now - timedelta(days=5))
    new = _rec(store, PID, "新しい", at=now - timedelta(days=1))
    kw = {"actor_id": PID, "actor_raw": None, "reason": "private", "actor_not_before": floor}
    assert store.withdraw(old, **kw) is False
    assert store.withdraw(new, **kw) is True
    # 床なし（in-process viewer）は従来どおり
    assert store.withdraw(old, **{**kw, "actor_not_before": None}) is True
    if client is not None:
        sql = next(s for s in client.sqls if "INSERT INTO" in s and "usage_withdrawals" in s)
        assert "l.occurred_at >= @actor_not_before" in sql


# --- 5. 本番の入口は identity_mapped=True の viewer を作る ------------------------------


def test_request_context_factory_builds_identity_mapped_viewers(monkeypatch):
    import waca_loop.server as server

    persons = _persons()
    _claim(persons, "sub-new", at=datetime.now(UTC))
    monkeypatch.setattr(server, "_current_subject", lambda: "sub-new")
    monkeypatch.setattr(server, "context_for", lambda viewer, runtime: viewer)
    viewer = server.build_request_context_factory(SimpleNamespace(persons=persons))()
    assert viewer.identity_mapped is True and viewer.person_id == PID


def test_production_stdio_context_builds_identity_mapped_viewer(monkeypatch):
    import waca_loop.auth.oauth as oauth
    import waca_loop.config as config
    import waca_loop.server as server

    persons = _persons()
    _claim(persons, "sub-new", at=datetime.now(UTC))

    class _OAuth:
        def __init__(self, settings):
            pass

        def credentials(self):
            return None

        def identity(self):
            return Identity(subject="sub-new")

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(
        bq_auth="adc", workspace_domains_set=frozenset()))
    monkeypatch.setattr(oauth, "OAuthManager", _OAuth)
    monkeypatch.setattr(server, "build_shared_runtime",
                        lambda credentials=None: SimpleNamespace(persons=persons))
    monkeypatch.setattr(server, "context_for", lambda viewer, runtime: viewer)
    viewer = server.build_production_context()
    assert viewer.identity_mapped is True and viewer.person_id == PID
