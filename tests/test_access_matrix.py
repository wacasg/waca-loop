"""ACCESS_MATRIX — 文書に書いた既定と落とし穴が、実装と一致していることを固定する.

`docs/ACCESS_MATRIX.md` は**実装を記述した文書**であって、実装を上書きするものではない。
記述が実装と食い違ったまま残ると、その表を読んで設計する次の人（ERN の `find_expert` が
まさにそれ）が間違った前提で作る。だからここで両者を突き合わせておく。

固定するのは3つ:
- §0 の落とし穴3つ（tier の帰着 / `org` を owner にした罠 / org tier の owner は柵でない）
- §1 の break-glass（既定値、名簿と find_expert が対象外であること）
- §2 の既定（種別ごとの tier / audience / sensitivity と追加ゲート）
"""

from __future__ import annotations

from waca_loop.config import Settings
from waca_loop.models import (
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Viewer,
)
from waca_loop.retrieval.acl import (
    BOARD_UNIT,
    ORG_WIDE_UNITS,
    OWNER_UNIT,
    STEWARD_UNIT,
    visible_to,
    widen_refusal,
)


def _v(*units: str, member: bool = True, person_id: str = "p1") -> Viewer:
    return Viewer(
        subject=f"sub-{person_id}", person_id=person_id,
        is_org_member=member, units=frozenset(units),
    )


def _scope(tier: str, audience: str = "internal", sensitivity: str = "normal") -> ScopeTags:
    return ScopeTags(ScopeTier(tier), ScopeAudience(audience), ScopeSensitivity(sensitivity))


# =====================================================================================
# §0 落とし穴1 — personal / team / dept は同じ1つの規則に潰れる
# =====================================================================================

def test_personal_tier_is_not_narrower_than_dept_in_practice():
    """C(負): `personal` と書いても、その owner_unit の全員に見える.

    DDL に per-person の所有者列が無いので、`_tier_ok` は org 以外をすべて
    「viewer が owner_unit に居るか」に帰着させる。**表の tier 欄を「意図」で読むと
    取り違える** — 文書 §0 落とし穴1 はこの事実を書いたもの。
    """
    colleague = _v("secretariat", person_id="p_other")
    for tier in ("personal", "team", "dept"):
        assert visible_to(_scope(tier), "secretariat", colleague), tier


def test_a_scoped_row_with_no_owner_is_visible_to_nobody():
    """A(正): owner_unit が無い非 org 行は誰にも見えない（fail-closed）."""
    for tier in ("personal", "team", "dept"):
        assert not visible_to(_scope(tier), None, _v("secretariat")), tier


# =====================================================================================
# §0 落とし穴2 — org を owner にすると実質 org 全体公開
# =====================================================================================

def test_owning_a_row_with_org_reaches_everyone_despite_the_scope():
    """C(負): 全員の unit_path が `org` を持つので、dept と書いても全社に届く.

    文書 §0 落とし穴2。読みゲート（`visible_to`）は `ORG_WIDE_UNITS` を意図的に参照して
    いないため、ここでは罠が生きている。**表で org 相当を指すときは tier=org と書く。**
    """
    assert "org" in ORG_WIDE_UNITS
    stranger = _v("org", "tech", person_id="p_stranger")
    assert visible_to(_scope("dept"), "org", stranger)      # 絞ったつもりが届く
    assert not visible_to(_scope("dept"), "secretariat", stranger)   # 本来の絞り


# =====================================================================================
# §0 落とし穴3 — org tier の owner_unit は柵ではない
# =====================================================================================

def test_org_tier_ignores_owner_unit_entirely():
    """A(正): org tier では owner_unit を見ない（保守者の記録であって権限でない）.

    たとえば org tier ＋ owner_unit='kyozai' の用語は、全社が読める。
    """
    outsider = _v("tech", person_id="p_outsider")
    assert visible_to(_scope("org"), "kyozai", outsider)
    assert visible_to(_scope("org"), None, outsider)


# =====================================================================================
# §1 break-glass
# =====================================================================================

def test_export_admin_units_has_a_real_value():
    """A(正): `export_admin` が break-glass 保有 unit（既定が空でない）."""
    assert Settings().export_admin_units_set == frozenset({"export_admin"})


def test_the_roster_and_find_expert_stay_outside_break_glass():
    """C(負): 名簿と `find_expert` は break-glass の対象外.

    `export_admin` を `export_admin_units` に置いても、名簿の除外は解除されない。
    `lookup_terms` は persons を**二重に**クランプしており（語彙を持ち出す理由が
    名簿のダンプを許可しない）、SPEC_phase3 §3 は
    `find_expert` を「persons と同じ扱い＝対象外」と明記している。
    この3つが揃って「対象外」を支えているので、どれが崩れてもここで落ちる。
    """
    import inspect

    from waca_loop.server import _K_CAPS, _NO_BREAK_GLASS, ServerContext

    assert _K_CAPS["persons"] == 10          # 承認済みの上限。黙って上げない
    assert _K_CAPS["find_expert"] == 3       # 〃（人の順位付けをしない・SPEC_phase3 §3）

    # **構造で**締める。`find_expert` の除外が「`_clamp_k` を reason 無しで呼んでいる」
    # ことだけに乗っていると、他の5ツールと同じ流儀で reason を通す改修が入った瞬間に
    # 3 → 30 が黙って開く。
    # export_my_usage(v1.1.0)は本人の記録しか返さない書き出しで、超過させる理由が無い。
    # 締める側への追加なので、対象外の集合が広がっただけ（緩めてはいない）。
    assert _NO_BREAK_GLASS == frozenset({"persons", "find_expert", "export_my_usage"})

    src = inspect.getsource(ServerContext.lookup_terms)
    assert 'min(self._clamp_k(k, "persons"), _K_CAPS["persons"])' in src


def test_the_no_break_glass_tools_cannot_be_exceeded_even_with_everything(embedder,
                                                                         viewer_member):
    """C(負): 保有 unit ＋ 理由 ＋ 生きた監査 sink が揃っても、名簿系は超えない.

    3条件が揃うのは break-glass が**効く**条件そのもの。それでも上限のままであることを
    見て初めて「対象外」と言える。
    """
    import dataclasses

    from waca_loop.grounding.terms import InMemoryTermStore
    from waca_loop.ledger.tool_events import InMemoryToolEventStore
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore
    from waca_loop.server import _K_CAPS, ServerContext

    admin = dataclasses.replace(viewer_member, units=frozenset({"export_admin"}))
    trail = InMemoryToolEventStore()
    ctx = ServerContext(
        term_store=InMemoryTermStore([], embedder),
        usage_store=InMemoryUsageLogStore(), embedder=embedder, viewer=admin,
        tool_event_store=trail,
        export_admin_units=frozenset({"export_admin"}),
    )

    for tool in ("persons", "find_expert"):
        got = ctx._clamp_k(10_000, tool, "監査のため全件見たい")   # noqa: SLF001
        assert got == _K_CAPS[tool], tool
    # 超過していないので、監査行も立たない
    assert trail.rows == []

    # 対象外でないツールは、同じ条件で超過できる（過剰に締めていない）
    assert ctx._clamp_k(10_000, "search_sfaq", "監査のため") > _K_CAPS["search_sfaq"]  # noqa: SLF001


def test_break_glass_needs_a_reason_and_a_live_audit_sink(embedder, viewer_member):
    """C(負): 保有 unit に居るだけでは超過できない（理由 ＋ 監査 sink が要る）."""
    import dataclasses

    from waca_loop.grounding.terms import InMemoryTermStore
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore
    from waca_loop.server import _K_CAPS, ServerContext

    admin = dataclasses.replace(viewer_member, units=frozenset({"export_admin"}))
    cap = _K_CAPS["search_sfaq"]

    no_sink = ServerContext(
        term_store=InMemoryTermStore([], embedder),
        usage_store=InMemoryUsageLogStore(), embedder=embedder, viewer=admin,
        export_admin_units=frozenset({"export_admin"}),
    )
    assert no_sink._clamp_k(10_000, "search_sfaq", "監査のため") == cap   # noqa: SLF001
    assert no_sink._clamp_k(10_000, "search_sfaq") == cap                 # noqa: SLF001 理由なし


def test_break_glass_actually_exceeds_and_always_leaves_one_audit_row(embedder, viewer_member):
    """A(正): 3条件が揃えば超過する — かつ超過1回につき監査行が1行残る.

    負の側（理由なし・sink なし）だけを固定すると、「常に cap を返す」実装でも緑になる。
    break-glass が**効く**ことと、効いたら**必ず記録が残る**ことを、ここで表明する。
    超過にも天井があること（cap × _K_OVERRIDE_MULTIPLIER）も同時に固定する。
    """
    import dataclasses

    from waca_loop.grounding.terms import InMemoryTermStore
    from waca_loop.ledger.tool_events import InMemoryToolEventStore
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore
    from waca_loop.server import _K_CAPS, _K_OVERRIDE_MULTIPLIER, ServerContext

    admin = dataclasses.replace(viewer_member, units=frozenset({"export_admin"}))
    trail = InMemoryToolEventStore()
    ctx = ServerContext(
        term_store=InMemoryTermStore([], embedder),
        usage_store=InMemoryUsageLogStore(), embedder=embedder, viewer=admin,
        tool_event_store=trail,
        export_admin_units=frozenset({"export_admin"}),
    )
    cap = _K_CAPS["search_sfaq"]

    got = ctx._clamp_k(cap + 5, "search_sfaq", "監査のため棚卸しする")   # noqa: SLF001
    assert got == cap + 5                       # 効いている
    assert len(trail.rows) == 1                 # 1回につき1行
    assert trail.rows[0]["tool"] == "k_override"

    # 超過にも天井がある
    assert ctx._clamp_k(10_000, "search_sfaq", "全件監査") == (  # noqa: SLF001
        cap * _K_OVERRIDE_MULTIPLIER
    )
    assert len(trail.rows) == 2

    # 記録は件数だけで、呼び出し側の文面を持たない
    assert "棚卸し" not in str(trail.rows[0])


def test_a_viewer_outside_the_holder_unit_never_exceeds(embedder, viewer_member):
    """C(負): 保有 unit の外の閲覧者は、理由を書いても超過できない."""
    from waca_loop.grounding.terms import InMemoryTermStore
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore
    from waca_loop.server import _K_CAPS, ServerContext

    ctx = ServerContext(
        term_store=InMemoryTermStore([], embedder),
        usage_store=InMemoryUsageLogStore(), embedder=embedder, viewer=viewer_member,
        export_admin_units=frozenset({"export_admin"}),
    )
    assert ctx._clamp_k(10_000, "search_sfaq", "理由はある") == _K_CAPS["search_sfaq"]  # noqa: SLF001


# =====================================================================================
# §2 種別ごとの既定
# =====================================================================================

def test_sensitive_rows_need_the_unit_or_an_explicit_grant():
    """A(正): sensitivity=sensitive は既定ではなく「追加の締め」."""
    outsider = _v("tech", person_id="p_out")
    granted = Viewer(
        subject="s", person_id="p_g", is_org_member=True, units=frozenset({"tech"}),
        allowed_sensitive_units=frozenset({"lab"}),
    )
    scope = _scope("org", "internal", "sensitive")
    assert not visible_to(scope, "lab", outsider)
    assert visible_to(scope, "lab", granted)
    assert visible_to(scope, "lab", _v("lab"))


def test_public_audience_reaches_a_non_member():
    """C(負): audience=public は**組織外にも届く**.

    文書 §2.2 と §2.3 でこれを例外として挙げているのは、`_audience_ok` の PUBLIC が
    `is_org_member` を見ないため。既定を internal にしている理由でもある。
    """
    stranger = _v(member=False, person_id="p_stranger")
    assert visible_to(_scope("org", "public"), None, stranger)
    assert not visible_to(_scope("org", "internal"), None, stranger)


def test_the_board_cannot_be_widened_to_the_org_tier():
    """A(正): 理事会所有の行は org へ広げられない（公開が既定の場に構造的な締め）."""
    assert widen_refusal(BOARD_UNIT, ScopeSensitivity.NORMAL) is not None
    assert widen_refusal("secretariat", ScopeSensitivity.NORMAL) is None


def test_sensitive_cannot_be_widened_to_the_org_tier():
    assert widen_refusal("lab", ScopeSensitivity.SENSITIVE) is not None


def test_role_units_named_in_the_matrix_exist_in_code():
    """C(負): 文書が名指しした role unit が実装から消えていない.

    これらは組織シートに無く、`manual_units` の carry-forward で
    再同期を生き延びる扱い。**消さないこと**を文書に書いた以上、消えたら落ちること。
    """
    assert STEWARD_UNIT == "steward"
    assert OWNER_UNIT == "owner"
    assert BOARD_UNIT == "board"


def test_sheet_derivable_units_never_swallow_the_hand_granted_ones():
    """A(正): シートから導出できない unit は再同期で残る（§4）."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.sync_persons_from_sheet import manual_units

    sheet_derivable = {"secretariat", "kyozai", "org"}
    got = manual_units(
        ["org", "secretariat", "steward", "export_admin"],
        sheet_derivable,
    )
    assert got == ["steward", "export_admin"]


def test_improvement_requests_default_to_dept_not_org():
    """A(正): 改善要望だけが dept 既定（起票した単位の文脈を持つため）."""
    from waca_loop.improvement.requests import _scope_of

    assert _scope_of({}).tier is ScopeTier.DEPT
    assert _scope_of({}).audience is ScopeAudience.INTERNAL
    assert _scope_of({}).sensitivity is ScopeSensitivity.NORMAL


def test_sfaq_candidate_rows_are_never_served():
    """C(負): `candidate` は配信されない."""
    from waca_loop.knowledge.acl import SERVABLE_STATUSES

    assert "candidate" not in SERVABLE_STATUSES
    assert set(SERVABLE_STATUSES) == {"provisional", "approved", "contested"}


def test_sfaq_permission_gate_fails_closed_on_an_unknown_provenance():
    """C(負): 由来が不明な行は本人許諾を要求する（fail-closed）."""
    from waca_loop.knowledge.acl import question_visible_to

    row = {
        "status": "approved", "scope_tier": "org", "scope_audience": "internal",
        "scope_sensitivity": "normal", "owner_unit": None,
        "provenance": "brand_new_kind", "perm_corporate": True, "perm_individual": False,
    }
    assert not question_visible_to(row, _v("secretariat"))
    assert question_visible_to({**row, "perm_individual": True}, _v("secretariat"))


def test_the_roster_gate_does_not_use_the_three_axes():
    """A(正): 名簿は3軸を持たず、同意・在籍・メンバー資格で決まる（§2.1 の「✗」）."""
    from waca_loop.grounding.persons import InMemoryPersonStore  # noqa: F401
    from waca_loop.models import Person
    from waca_loop.retrieval.acl import person_visible_to

    active = Person(person_id="p_a", display_name="A", consent_internal=True)
    assert person_visible_to(active, _v("tech"))
    assert not person_visible_to(active, _v(member=False))
    assert not person_visible_to(
        Person(person_id="p_f", display_name="F", status="frozen"), _v("tech")
    )
    opted_out = Person(person_id="p_o", display_name="O", consent_internal=False)
    assert not person_visible_to(opted_out, _v("tech"))
    assert person_visible_to(opted_out, _v("tech", person_id="p_o"))   # 本人は見える


# =====================================================================================
# §2.3 ティア別 — Lite で落ちる保証
# =====================================================================================

def test_ern_uses_no_vector_search_so_the_lite_note_is_accurate():
    """A(正): ERN はベクトル検索を使わない.

    「ERN は不可（ベクトル検索前提）」ではない。`SPEC_phase3` §2.1 は
    `embedding` を「**リコール専用。スコアには一切寄与しない**」と定めている。実装が
    実際にベクトルを読んでいないことを、ここで固定しておく — 文書だけだと、あとで
    誰かが距離をスコアに足したときに訂正のほうが古くなる。
    """
    import ast
    import importlib
    import inspect
    import pkgutil

    import waca_loop.ern

    reads: list[str] = []
    for info in pkgutil.iter_modules(waca_loop.ern.__path__):
        mod = importlib.import_module(f"waca_loop.ern.{info.name}")
        src = inspect.getsource(mod)
        assert "VECTOR_SEARCH" not in src, f"{info.name} がベクトル検索を呼んでいる"
        # `embedding` を**読んで**いないこと。書き込み（INSERT のバインド）は許す。
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Attribute) and node.attr == "embedding":
                reads.append(f"{info.name}:{node.lineno}")

    # store.add の INSERT パラメータ 1 箇所だけ（＝ 書き込み）。増えていたら見直すこと。
    assert len(reads) == 1, reads


def test_the_sql_side_gates_exist_so_lite_has_something_to_lose():
    """A(正): L1 が「落ちる」と言えるのは、Standard に SQL 側ゲートが在るから.

    表の Lite 欄は「Standard にはあるが Lite で無くなる」という主張なので、
    **Standard 側の存在**を確かめておかないと、書いてあることが空証明になる。
    """
    from waca_loop.knowledge.acl import question_where_clause
    from waca_loop.retrieval.acl import acl_where_clause, person_where_clause

    v = _v("kyozai")
    for fn in (acl_where_clause, person_where_clause, question_where_clause):
        sql, params = fn(v)
        assert sql.strip(), fn.__name__
        assert params, fn.__name__

    # ERN 宣言も SQL 側を持つ
    import inspect

    from waca_loop.ern import store as ern_store

    assert "person_where_clause" in inspect.getsource(ern_store)


def test_the_append_only_tables_rely_on_bigquery_for_enforcement():
    """A(正): L2 の対象（台帳・ERN 監査）に、アプリ側の削除 API が無い.

    Lite で落ちるのは「**強制**」であって「設計」ではない — 設計上どこにも delete が
    無いことは Lite でも変わらず、変わるのは「消せてしまう人が居る」かどうか。
    """
    from waca_loop.ern.store import BigQueryErnAuditStore, InMemoryErnAuditStore
    from waca_loop.ledger.usage_log import BigQueryUsageLogStore

    for cls in (InMemoryErnAuditStore, BigQueryErnAuditStore):
        assert {n for n in dir(cls) if not n.startswith("_")} == {"record"}

    # 台帳は R6（本人の取り下げ）で delete を持つ — そこは append-only の例外として
    # 既に設計に入っている。ERN 監査には無い、という差を確認する。
    assert hasattr(BigQueryUsageLogStore, "delete")
