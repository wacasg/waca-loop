"""E4 — 未同意者は絶対に出ない（INV-7・SPEC_phase3 §4c）.

**両ゲートで**見る。(a) InMemory 経路の出力、(b) BigQuery 用 WHERE 句の文字列に consent 条件が
含まれること。片方だけだと §3.3 の半分が欠け、この store を直接呼ぶ経路が1つ増えた瞬間に、
オプトアウトした人の person_id が外へ出る。

**シグナルを最強にした状態で検査する**のが要点 — 弱いせいで出ていないのか、ゲートが効いて
いるのかを取り違えないため。
"""

from __future__ import annotations

from waca_loop.models import FbType, Viewer


def _strong(ctx, seed, person_id, times=5):
    for _ in range(times):
        seed(ctx, authors=(person_id,), fb_type=FbType.DECISION)


# --- (a) in-process 側 -------------------------------------------------------------------

def test_an_opted_out_person_never_appears_however_strong_the_signal(ern_context, ern_seed_fn):
    """C(負): consent_internal=false は最強シグナルでも出ない."""
    _strong(ern_context, ern_seed_fn, "p_optout", times=10)
    _strong(ern_context, ern_seed_fn, "p_ok", times=1)

    ids = [c["person_id"] for c in ern_context.find_expert("採点基準")["candidates"]]
    assert "p_optout" not in ids
    assert "p_ok" in ids, "同意者まで消えていたら、それはゲートではなく故障"


def test_an_opted_out_person_does_not_even_appear_to_themselves(ern_context, ern_seed_fn):
    """C(負): オプトアウトした本人が自分で検索しても、自分は候補にならない.

    名簿の解決（`person_visible_to`）には「本人は自分を見られる」例外があり、それは正しい —
    自分の登録内容を確認できないほうが不便で、誰にも新しい事実は開かない。
    **ERN はその例外を継がない。** `consent_internal` は「社内エキスパート探索に出さないで
    くれ」という表明そのものなので、本人が呼んだときだけ自分が返るのは、降ろしたはずの機能が
    本人にだけ動き続けている状態になる。SPEC_phase3 §4c は3条件を「すべて」と書いている。
    """
    import dataclasses

    _strong(ern_context, ern_seed_fn, "p_optout", times=5)
    as_self = dataclasses.replace(
        ern_context,
        viewer=Viewer(subject="sub-optout", person_id="p_optout",
                      is_org_member=True, units=frozenset({"org"})),
    )
    assert as_self.find_expert("採点基準")["candidates"] == []


def test_the_roster_lookup_keeps_its_self_exception(ern_persons):
    """A(正): 名簿側の例外は**残す** — ERN だけが継がない（過剰に締めていない）."""
    from waca_loop.retrieval.acl import (
        person_disclosable_as_expert,
        person_visible_to,
    )

    optout = next(p for p in ern_persons if p.person_id == "p_optout")
    themself = Viewer(subject="s", person_id="p_optout", is_org_member=True,
                      units=frozenset())

    assert person_visible_to(optout, themself)                  # 名簿では見える
    assert not person_disclosable_as_expert(optout, themself)   # ERN では出ない


def test_a_frozen_person_never_appears(ern_context, ern_seed_fn):
    """C(負): 退職者は出ない（同意していても）."""
    _strong(ern_context, ern_seed_fn, "p_frozen", times=10)
    ids = [c["person_id"] for c in ern_context.find_expert("採点基準")["candidates"]]
    assert "p_frozen" not in ids


def test_a_person_absent_from_the_roster_never_appears(ern_context, ern_seed_fn):
    """C(負): 名簿に無い author_id は出さない（在籍も同意も確かめられない）."""
    _strong(ern_context, ern_seed_fn, "p_ghost", times=10)
    assert ern_context.find_expert("採点基準")["candidates"] == []


def test_a_non_member_gets_nothing(ern_context, ern_seed_fn):
    """C(負): 非 org メンバーは常に空（fail closed）."""
    import dataclasses

    _strong(ern_context, ern_seed_fn, "p_ok", times=5)
    outsider = dataclasses.replace(
        ern_context, viewer=Viewer(subject="x", person_id=None, is_org_member=False,
                                   units=frozenset()),
    )
    assert outsider.find_expert("採点基準")["candidates"] == []


def test_the_engine_itself_refuses_a_non_member(ern_persons):
    """C(負): エンジン**単体**でも非 org メンバーには空を返す.

    `ServerContext.find_expert` にも同じ判定がある（§3.3 の二重化）。だが上位の検査だけだと、
    エンジン側の判定を外しても緑のまま — 直接 `build_candidates` を呼ぶ経路が1つ増えた
    瞬間に漏れる。二重化は「両方が独立に効く」ことが表明されて初めて二重化である。
    """
    from datetime import UTC, datetime

    from waca_loop.ern.engine import build_candidates

    people = {p.person_id: p for p in ern_persons}
    outsider = Viewer(subject="x", person_id=None, is_org_member=False, units=frozenset())

    got = build_candidates(
        viewer=outsider, topic="採点基準", questions=[], feedback_by_question={},
        people=people, declared={}, half_life_days=180, now=datetime.now(UTC),
        limit=3,
        role_fallback=["p_ok"],      # 役職フォールバックまで与えても空
    )
    assert got == []


def test_the_assertion_store_also_hides_an_opted_out_person(ern_context):
    """C(負): 宣言プロファイル**単体**でも、未同意者の person_id を返さない.

    「あとで engine が person_visible_to に掛けるから安全」では §3.3 の片側しか無い。
    """
    from waca_loop.ern.store import ErnAssertion

    store = ern_context.ern_assertion_store
    for pid in ("p_ok", "p_optout", "p_frozen"):
        store.add(ErnAssertion(assertion_id=f"a_{pid}", person_id=pid, topic="採点基準"))

    assert store.persons_for_topic("採点基準", ern_context.viewer) == ["p_ok"]
    assert set(store.topics_by_person(ern_context.viewer)) == {"p_ok"}


# --- (b) SQL 側 --------------------------------------------------------------------------

def test_the_bigquery_where_clause_carries_the_consent_condition():
    """A(正): BigQuery 側の WHERE に consent 条件が**文字列として**入っている（E4(b)）."""
    from waca_loop.config import Settings
    from waca_loop.ern.store import BigQueryErnAssertionStore

    class _Job:
        def result(self):
            return iter([])

    class _Rec:
        def __init__(self):
            self.sqls = []

        def query(self, sql, job_config=None):
            self.sqls.append(sql)
            return _Job()

    settings = Settings()
    settings.gcp_project = "demo"
    rec = _Rec()
    store = BigQueryErnAssertionStore(rec, settings)
    viewer = Viewer(subject="s", person_id="p_asker", is_org_member=True, units=frozenset())

    store.persons_for_topic("採点基準", viewer)
    store.topics_by_person(viewer)

    assert rec.sqls
    for sql in rec.sqls:
        assert "consent_internal" in sql, sql
        assert "status = 'active'" in sql, sql
        # 相関の等式が無いと「見える人が1人でもいれば全部通る」形になる。
        assert "p.person_id = a.person_id" in sql, sql


def test_the_sql_gate_and_the_in_process_gate_come_from_the_same_place():
    """C(負): 2つのゲートが別々に書かれていない（片方だけ緩むのを防ぐ）.

    どちらも `retrieval.acl` の `person_visible_to` / `person_where_clause` を呼ぶ。
    ERN 側に判定ロジックを写経していたら、ここが崩れる。
    """
    import ast
    import inspect

    from waca_loop.ern import store as ern_store

    src = inspect.getsource(ern_store)
    assert "person_visible_to" in src
    assert "person_where_clause" in src

    # 自前で consent 列を見ていない。**コードとして**触っていないことを AST で確かめる —
    # 文字列検索だと、その旨を説明した docstring に反応してしまう。
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert "consent_internal" not in names
