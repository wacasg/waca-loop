"""名簿(persons)のゲート — T2 の拡張（§3.3 二重で効かせる）.

防ぐ欠陥は2つ:
  1. `BigQueryPersonStore.search` の WHERE に ACL が無い（SQLゲート欠落）
  2. `lookup_terms` の人物ヒットが `ScopeTags(ORG, INTERNAL, NORMAL)` をハードコードして
     `acl_filter` を素通り＝オプトアウトも退職者もゲートを通らない

「同じユニットの同僚だけ見える」までは絞らない: メンバーは全社の人物名を解決できること。
それがこの層の目的（誰のことを言っているのか分からないままAIが答えるのを防ぐ・§4.2）であり、
`consent_internal` も DDL で「社内エキスパート探索・既定true」と定義されている。
**「名簿になる」問題は"誰を名指しできるか"ではなく"1コールで何件出るか"の問題**であり、
`_K_CAPS["persons"]`（FLOOR-02）で縛る。ここで縛るのは筋が悪い。

したがってこのゲートが担うのは3つだけ: 非メンバーには何も出さない／frozen(退職者)は
出さない／オプトアウトは本人以外に対して効く。
"""

from __future__ import annotations

from waca_loop.grounding.persons import InMemoryPersonStore
from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.models import Kind, Person, Viewer
from waca_loop.retrieval.acl import person_visible_to, person_where_clause
from waca_loop.server import _K_CAPS, ServerContext

ROSTER = [
    Person(person_id="p_sec1", display_name="事務局の田中", unit_path=["secretariat",
                                                                     "org"]),
    Person(person_id="p_sec2", display_name="事務局の佐藤", unit_path=["secretariat",
                                                                     "org"]),
    Person(person_id="p_kyo", display_name="教材の鈴木", unit_path=["kyozai", "org"]),
    Person(person_id="p_quiet", display_name="事務局の高橋", unit_path=["secretariat",
                                                                     "org"],
           consent_internal=False),
    Person(person_id="p_gone", display_name="事務局の退職者", unit_path=["secretariat",
                                                                      "org"],
           status="frozen"),
]


def _viewer(person_id: str, *units: str, member: bool = True) -> Viewer:
    return Viewer(subject=f"sub-{person_id}", person_id=person_id,
                  is_org_member=member, units=frozenset(units))


def _ctx(term_store, embedder, viewer, roster=None) -> ServerContext:
    return ServerContext(
        term_store=term_store, usage_store=InMemoryUsageLogStore(), embedder=embedder,
        viewer=viewer, person_store=InMemoryPersonStore(persons=roster or ROSTER),
    )


SEC = _viewer("p_sec1", "secretariat", "org")
KYO = _viewer("p_kyo", "kyozai", "org")


def test_members_can_resolve_colleagues_across_the_whole_association():
    """B: 部門をまたいで人物名を解決できること — ここは絞らない."""
    assert person_visible_to(ROSTER[1], SEC)      # 同じ事務局
    assert person_visible_to(ROSTER[2], SEC)      # 教材部門 — 部門が違っても解決できる
    assert person_visible_to(ROSTER[0], KYO)      # 逆向きも同じ


def test_optout_and_frozen_and_self():
    """B: 自分は常に見える／consent_internal=false と frozen(退職者)は出さない."""
    quiet_self = _viewer("p_quiet", "secretariat", "org")
    assert person_visible_to(ROSTER[3], quiet_self)   # オプトアウトしていても自分は見える
    assert not person_visible_to(ROSTER[3], SEC)      # 本人が拒否していれば他人には出さない
    assert not person_visible_to(ROSTER[3], KYO)
    assert not person_visible_to(ROSTER[4], SEC)      # frozen(退職者)


def test_non_member_gets_no_roster_at_all():
    """B: 組織メンバーでない閲覧者には名簿を一行も返さない(自分の行すら)."""
    outsider = Viewer.anonymous(subject="sub_hashed")
    for p in ROSTER:
        assert not person_visible_to(p, outsider)
    _, params = person_where_clause(outsider)
    assert params["acl_p_is_org_member"] is False     # SQL 側も同じ結論に落ちる


def test_lookup_terms_resolves_names_but_cannot_dump_the_roster(term_store, embedder):
    """B+D: 名前は引ける。ただし1コールで名簿にはならない(件数で縛る)."""
    ctx = _ctx(term_store, embedder, SEC)
    names = [h["term"] for h in ctx.lookup_terms("事務局", 100000)["hits"]]
    assert "事務局の佐藤" in names          # 同僚は解決できる
    assert "事務局の高橋" not in names      # オプトアウト
    assert "事務局の退職者" not in names    # frozen

    # 部門をまたいでも解決できる
    kyo_names = [h["term"] for h in _ctx(term_store, embedder, KYO).lookup_terms("事務局")["hits"]]
    assert "事務局の田中" in kyo_names

    # 量は FLOOR-02 の上限で縛られる
    many = [Person(person_id=f"p{i}", display_name=f"職員{i}", unit_path=["kyozai"])
            for i in range(40)]
    big = _ctx(term_store, embedder, SEC, roster=many)
    person_hits = [h for h in big.lookup_terms("職員", 100000)["hits"]
                   if h["kind"] == Kind.PERSON.value]
    assert len(person_hits) <= _K_CAPS["persons"]


def test_store_fails_closed_without_a_viewer():
    """B: viewer を渡さない呼び出しは空を返す — 素の名簿検索の口を残さない."""
    assert InMemoryPersonStore(persons=ROSTER).search("事務局", 100) == []


def test_blank_query_never_enumerates():
    """B: 空・空白のクエリで名簿を引けない(BQ の STRPOS(x,'')=1 の落とし穴を両実装で塞ぐ)."""
    store = InMemoryPersonStore(persons=ROSTER)
    for blank in ("", "   ", "\t"):
        assert store.search(blank, 100000, SEC) == []


def test_viewer_without_person_id_matches_nobody_via_self():
    """B: person_id が無い閲覧者が空文字の person_id 行に自己一致しない(両ゲートで一致)."""
    ghost = Viewer(subject="sub-ghost", person_id=None, is_org_member=True,
                   units=frozenset({"org"}))
    empty_id = Person(person_id="", display_name="名無し", unit_path=["secretariat"],
                      consent_internal=False)
    assert not person_visible_to(empty_id, ghost)

    sql, params = person_where_clause(ghost)
    assert params["acl_p_self"] == ""
    assert "@acl_p_self != ''" in sql        # SQL 側も自己一致分岐を塞いでいる


def test_person_where_clause_mirrors_the_in_process_rule():
    """B: SQL 側の断片が、インプロセス側と同じ材料(active・メンバー・自分・同意)で組まれている."""
    sql, params = person_where_clause(SEC, alias="p")
    assert params["acl_p_self"] == "p_sec1"
    assert params["acl_p_is_org_member"] is True
    assert "p.status = 'active'" in sql
    assert "consent_internal" in sql
    # ユニットの一致は条件に入れない（全社で名前解決できるのが仕様）
    assert "unit_path" not in sql
