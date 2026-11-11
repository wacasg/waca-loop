"""1コールの抽出量に上限を置く (FLOOR-02 コア資産の丸ごと持ち出し).

`k` をクライアント指定のまま `LIMIT @lim` / `VECTOR_SEARCH(top_k => @lim)` に渡すと、
上限も下限も無くなる。ACL の境界は越えないが、**1コールで部署のコーパスを丸ごと外部AI
ベンダのコンテキストへ移送できる**。台帳に `k` は残らないので、事後に k=5 と k=1000 を
区別できない。

平時上限は lookup_terms / search_sfaq が 20、人物が 10。
`export_admin_units` の閲覧者だけが reason 必須で超過でき、そのとき監査行が必ず1行残る。
"""

from __future__ import annotations

import pytest

from waca_loop.embeddings import HashingEmbedder
from waca_loop.grounding.persons import InMemoryPersonStore
from waca_loop.grounding.terms import InMemoryTermStore
from waca_loop.knowledge.store import InMemoryQuestionStore, new_id
from waca_loop.ledger.tool_events import InMemoryToolEventStore
from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.models import (
    Person,
    Provenance,
    Question,
    QuestionStatus,
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Term,
    Viewer,
)
from waca_loop.server import _K_CAPS, ServerContext

SEC = Viewer(subject="s", person_id="p_sec", is_org_member=True,
             units=frozenset({"secretariat", "org"}))
ADMIN = Viewer(subject="s2", person_id="p_admin", is_org_member=True,
               units=frozenset({"secretariat", "export_admin", "org"}))

ORG = ScopeTags(ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL)


def _ctx(viewer, *, admin_units=frozenset(), trail=None) -> ServerContext:
    embedder = HashingEmbedder(dim=64)
    terms = [
        Term(term_id=f"t{i}", term=f"用語{i}", definition="事務局の共通語彙", scope=ORG)
        for i in range(60)
    ]
    questions = [
        Question(question_id=new_id("q"), question_text=f"事務局の問い{i}", scope=ORG,
                 provenance=Provenance.CORPORATE_TRAINING, perm_corporate=True,
                 status=QuestionStatus.APPROVED)
        for i in range(60)
    ]
    persons = [
        Person(person_id=f"p{i}", display_name=f"事務局の職員{i}",
               unit_path=["secretariat", "org"])
        for i in range(40)
    ]
    return ServerContext(
        term_store=InMemoryTermStore(terms, embedder),
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=viewer,
        person_store=InMemoryPersonStore(persons=persons),
        knowledge_store=InMemoryQuestionStore(questions, embedder),
        tool_event_store=trail,
        export_admin_units=admin_units,
    )


def test_a_ordinary_viewer_is_capped_however_large_k_is():
    """(a) 一般の閲覧者は k をいくら大きくしても平時上限までしか取り出せない."""
    ctx = _ctx(SEC)
    assert len(ctx.lookup_terms("用語", 100000)["hits"]) <= _K_CAPS["lookup_terms"]
    assert len(ctx.search_sfaq("問い", 100000)["hits"]) <= _K_CAPS["search_sfaq"]

    # 名簿はさらに厳しい上限(人物ヒットのみを数える)
    person_hits = [h for h in ctx.lookup_terms("職員", 100000)["hits"]
                   if h["kind"] == "person"]
    assert len(person_hits) <= _K_CAPS["persons"]


def test_a_cap_does_not_change_ordinary_calls():
    """(a) 上限以下の呼び出しは素通り — 平時の使い勝手は変えない."""
    ctx = _ctx(SEC)
    assert len(ctx.lookup_terms("用語", 5)["hits"]) == 5
    assert ctx._clamp_k(5, "lookup_terms") == 5      # noqa: SLF001
    assert ctx._clamp_k(0, "lookup_terms") == 1      # noqa: SLF001 下限
    assert ctx._clamp_k(None, "lookup_terms") == _K_CAPS["lookup_terms"]  # noqa: SLF001


def test_b_export_admin_may_exceed_and_the_audit_row_always_remains():
    """(b) export_admin は reason つきで超過できるが、監査行が必ず1行残る."""
    trail = InMemoryToolEventStore()
    ctx = _ctx(ADMIN, admin_units=frozenset({"export_admin"}), trail=trail)

    got = ctx._clamp_k(120, "search_sfaq", "監査のため事務局の判断を棚卸しする")  # noqa: SLF001
    assert got == 120

    rows = [r for r in trail.rows if r["tool"] == "k_override"]
    assert len(rows) == 1
    assert rows[0]["actor_id"] == "p_admin"
    assert rows[0]["detail"] == "search_sfaq:k=120:cap=20"
    # 監査行に理由の本文は入れない（set_session_scope と同じ流儀・内容は持たない）
    assert "棚卸し" not in (rows[0]["detail"] or "")


def test_b_the_override_itself_has_a_ceiling():
    """(b) 超過にも天井がある — 理由さえ書けば全量、にはしない(FLOOR-02)."""
    trail = InMemoryToolEventStore()
    ctx = _ctx(ADMIN, admin_units=frozenset({"export_admin"}), trail=trail)

    got = ctx._clamp_k(1000000, "search_sfaq", "全件監査")  # noqa: SLF001
    assert got == _K_CAPS["search_sfaq"] * 10
    # 監査行にも実際に出した件数が載る（要求値ではなく）
    assert [r["detail"] for r in trail.rows if r["tool"] == "k_override"] == [
        "search_sfaq:k=200:cap=20"
    ]


def test_c_reason_is_required_even_for_an_export_admin():
    """(c) reason 省略時は export_admin でも超過できない(＝上限まで絞られる)."""
    trail = InMemoryToolEventStore()
    ctx = _ctx(ADMIN, admin_units=frozenset({"export_admin"}), trail=trail)

    for missing in (None, "", "   "):
        assert ctx._clamp_k(500, "search_sfaq", missing) == _K_CAPS["search_sfaq"]  # noqa: SLF001
    assert [r for r in trail.rows if r["tool"] == "k_override"] == []


def test_c_no_audit_sink_means_no_break_glass():
    """(c) 監査行を残せない構成では、export_admin でも超過を許さない."""
    ctx = _ctx(ADMIN, admin_units=frozenset({"export_admin"}), trail=None)
    assert ctx._clamp_k(500, "search_sfaq", "理由あり") == _K_CAPS["search_sfaq"]  # noqa: SLF001


def test_c_admin_membership_is_required_not_just_a_reason():
    """(c) 一般の閲覧者は reason を書いても超過できない."""
    trail = InMemoryToolEventStore()
    ctx = _ctx(SEC, admin_units=frozenset({"export_admin"}), trail=trail)
    assert ctx._clamp_k(500, "search_sfaq", "理由はあります") == _K_CAPS["search_sfaq"]  # noqa: SLF001
    assert trail.rows == []


def test_unknown_tool_falls_back_to_the_tightest_default():
    """将来のツールを登録し忘れても、既定は最も狭い上限になる(fail closed)."""
    ctx = _ctx(SEC)
    # **登録されていない**名前を使う — 例が実在するツールになると、この検査は
    # 「既定へ落ちること」ではなく「そのツールの上限」を見てしまう。
    assert ctx._clamp_k(100000, "not_a_registered_tool") == 10   # noqa: SLF001  _K_CAP_DEFAULT


@pytest.mark.parametrize("tool,cap", sorted(_K_CAPS.items()))
def test_caps_are_the_approved_values(tool, cap):
    """上限値を固定する — 黙って緩めない.

    いずれも承認済みの値。sfaq_feedback / improvement_tasks はクライアント指定の k を持たない
    経路にも同じ上限を掛けたもの。

    improvement_requests は improvement_tasks に揃えた。
    要望一覧はインシデント由来の申告と違って機微度は低いが、部門の事情がにじむ本文を持つ
    ので、上限そのものは外さない。

    find_expert は **3**。根拠は「人の順位付けをしない」方針と
    SPEC_phase3 §3 で、名簿(10)よりさらに狭い。ERN は人を返すツールなので、5種別の中で
    いちばん締まった側に置く（docs/ACCESS_MATRIX.md §3）。
    """
    assert cap == {
        "lookup_terms": 20, "search_sfaq": 20, "persons": 10,
        "sfaq_feedback": 20, "improvement_tasks": 50, "improvement_requests": 50,
        "find_expert": 3,
    }[tool]


def test_roster_cap_is_not_break_glass_able(embedder):
    """D(負): 用語の持ち出しに与えた reason で、名簿の上限まで外れてはいけない."""
    trail = InMemoryToolEventStore()
    ctx = _ctx(ADMIN, admin_units=frozenset({"export_admin"}), trail=trail)

    hits = ctx.lookup_terms("職員", 150, reason="語彙の棚卸しのため")["hits"]
    assert len([h for h in hits if h["kind"] == "person"]) <= _K_CAPS["persons"]
    # 名簿側で break-glass が起きていない = 監査行は用語側の1本だけ
    overrides = [r["detail"] for r in trail.rows if r["tool"] == "k_override"]
    assert overrides == ["lookup_terms:k=150:cap=20"]


def test_break_glass_is_refused_when_the_audit_write_fails():
    """D(負): 監査行の書き込みが失敗したら超過を許さない(best-effort に流さない)."""
    class _BrokenTrail:
        def record(self, *a, **k):
            raise RuntimeError("streaming insert quota exceeded")

    ctx = _ctx(ADMIN, admin_units=frozenset({"export_admin"}), trail=_BrokenTrail())
    assert ctx._clamp_k(500, "search_sfaq", "理由あり") == _K_CAPS["search_sfaq"]  # noqa: SLF001
