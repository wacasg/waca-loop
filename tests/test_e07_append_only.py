"""E7 — シグナルは append-only.

update / delete の API を置かない。同じ検索を2回すれば2行になり、件数は**導出値**として
増える。行を書き換えて件数を表すと、証跡が「後から辻褄を合わせられるもの」になり、
③信頼度エンジンの定義（後から再現できること）が成り立たない。
"""

from __future__ import annotations

from waca_loop.ern.store import (
    BigQueryErnAssertionStore,
    BigQueryErnAuditStore,
    InMemoryErnAssertionStore,
    InMemoryErnAuditStore,
)

MUTATORS = ("update", "delete", "remove", "set_status", "upsert", "merge", "clear", "purge")


def _public(cls) -> set[str]:
    return {n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n))}


def test_no_store_exposes_a_mutator():
    """C(負): 4つのストアのどれにも書き換え/削除の API が無い."""
    for cls in (InMemoryErnAssertionStore, BigQueryErnAssertionStore,
                InMemoryErnAuditStore, BigQueryErnAuditStore):
        found = {m for m in _public(cls) if any(bad in m.lower() for bad in MUTATORS)}
        assert not found, (cls.__name__, found)


def test_the_store_pair_has_the_same_surface():
    """A(正): InMemory と BigQuery が同一サーフェス（INV-3）.

    片方にだけメソッドがあると、テストが通る経路と本番の経路が違うものになる。
    """
    mem = _public(InMemoryErnAssertionStore)
    bq = _public(BigQueryErnAssertionStore)
    assert mem == bq, {"only_in_memory": mem - bq, "only_in_bq": bq - mem}

    assert _public(InMemoryErnAuditStore) == _public(BigQueryErnAuditStore) == {"record"}


def test_recording_the_same_search_twice_makes_two_rows(ern_context, ern_seed_fn):
    """A(正): 同じ検索を2回すれば2行。1行を書き換えて回数を表さない."""
    ern_seed_fn(ern_context, authors=("p_ok",))
    ern_context.find_expert("採点基準")
    ern_context.find_expert("採点基準")

    rows = ern_context.ern_audit_store.rows
    assert len(rows) == 2
    assert rows[0].audit_id != rows[1].audit_id


def test_adding_the_same_assertion_twice_makes_two_rows():
    """A(正): 宣言も append-only（重複は集計側で畳む）."""
    from waca_loop.ern.store import ErnAssertion
    from waca_loop.models import Person

    person = Person(person_id="p_ok", display_name="同意 太郎", consent_internal=True)
    store = InMemoryErnAssertionStore(persons={"p_ok": person})
    for i in range(2):
        store.add(ErnAssertion(assertion_id=f"a{i}", person_id="p_ok", topic="採点基準"))

    from waca_loop.models import Viewer

    viewer = Viewer(subject="s", person_id="p_x", is_org_member=True, units=frozenset())
    # 行は2本だが、person_id の一覧は重複しない（畳むのは読む側）
    assert store.persons_for_topic("採点基準", viewer) == ["p_ok"]
    assert store.topics_by_person(viewer)["p_ok"] == ["採点基準", "採点基準"]
