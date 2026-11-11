"""MCP 契約のうち、白箱で固定しておく2点（data_kinds の正規化・SFAQ の可視性ゲート）.

どちらも、ストア単体では正しく見えても束ね層で外れたり、ツールの説明文の約束と実装が
ずれたりしうる種類の性質である。黒箱スイート（`test_b1_black_box_contract.py`）が全ツールを
機械的に叩くのに対し、ここでは個々の性質を直接表明する。
"""

from __future__ import annotations

import pytest

from waca_loop.guard import VALID_DATA_KINDS, normalize_data_kinds
from waca_loop.knowledge.store import InMemoryQuestionStore
from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.models import (
    Provenance,
    Question,
    QuestionStatus,
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Viewer,
)
from waca_loop.server import ServerContext

# --- data_kinds の表記ゆれで下限判定が無言で外れないこと -------------------------


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("PII", "pii"),
        ("Pii", "pii"),
        (" pii ", "pii"),
        ("個人情報", "pii"),
        ("CORE_ASSET", "core_asset"),
        ("コア資産", "core_asset"),
        ("core-asset", "core_asset"),
        ("最終判断", "final_judgment"),
        ("機密", "confidential"),
    ],
)
def test_common_spellings_still_fire_the_floor_check(written, expected):
    """日本語で動くクライアントは普通にこう書く。書き方で安全機能が消えてはいけない。"""
    kinds, unknown = normalize_data_kinds([written])
    assert kinds == [expected], f"{written!r} が {expected} に写らない"
    assert not unknown


def test_an_unknown_kind_is_recorded_but_reported_back():
    """見慣れない語は**弾かない**（弾くと台帳の行を失う）が、黙ってもいけない。

    語彙外は `internal` に寄せる — `none`（＝何も機微でない）に寄せると、未知の語が
    「安全」と読める向きに倒れる。倒すなら保守的な側へ、というのが R9 の設計。
    そのうえで、写せなかった原文を呼び手へ返す。
    """
    kinds, unmapped = normalize_data_kinds(["pii", "nuclear_codes"])
    assert "pii" in kinds
    assert "internal" in kinds, "語彙外を none 側（無害）に寄せている"
    assert unmapped == ["nuclear_codes"]


def test_a_value_mentioning_personal_information_is_not_filed_as_bland():
    """「個人情報を含む顧客リスト」のような自由記述が `internal` に埋もれないこと."""
    kinds, _ = normalize_data_kinds(["個人情報を含む顧客リスト"])
    assert kinds == ["pii"]


def test_a_value_saying_it_contains_no_personal_information_is_not_inverted():
    """「個人情報なし」が『個人情報』に化けないこと（部分一致の順序が効いている）."""
    kinds, _ = normalize_data_kinds(["個人情報なし"])
    assert kinds == ["internal"]


def _ctx(term_store, embedder, viewer, kstore=None) -> ServerContext:
    return ServerContext(
        term_store=term_store, usage_store=InMemoryUsageLogStore(),
        embedder=embedder, viewer=viewer, knowledge_store=kstore,
    )


def test_log_usage_tells_the_caller_which_kinds_did_not_count(term_store, embedder,
                                                                 viewer_member):
    out = _ctx(term_store, embedder, viewer_member).log_usage(
        use_category="l1", data_kinds=["個人情報", "宇宙機密"], summary="要約", client="test",
    )
    assert out["log_id"]                                   # 記録は通る（弾かない）
    assert out["unmapped_data_kinds"] == ["宇宙機密"]        # 写せなかった原文を名指しする
    assert "pii" in out["data_kinds_recorded"]             # 台帳に入った正規語彙を返す
    # 台帳に入るのは正典語彙だけ（自由記述は入らない）。`assert VALID_DATA_KINDS` では
    # **定数が真であることを言っているだけの飾り**になるので、集合で比べる。
    assert set(out["data_kinds_recorded"]) <= set(VALID_DATA_KINDS)
    assert "宇宙機密" not in out["data_kinds_recorded"]


# --- 読めない行は動かせないこと ----------------------------------------------------

ORG = ScopeTags(ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL)
DEPT_SENSITIVE = ScopeTags(ScopeTier.DEPT, ScopeAudience.INTERNAL, ScopeSensitivity.SENSITIVE)


def _steward(units=("steward", "dept_eng")) -> Viewer:
    return Viewer(subject="s", person_id="p_s", is_org_member=True, units=frozenset(units))


def _q(qid, scope, owner_unit) -> Question:
    return Question(
        question_id=qid, question_text="他部門の判断", scope=scope,
        provenance=Provenance.CORPORATE_TRAINING, owner_unit=owner_unit,
        perm_corporate=True, status=QuestionStatus.APPROVED,
    )


def test_a_steward_cannot_contest_a_question_they_cannot_read(term_store, embedder):
    """`set_sfaq_status` も、兄弟のツールと同じ可視性ゲートを通る.

    ゲートが無いと (a) 他部門の承認済みの判断を、読めない立場から contested に落とせる
    (b) 「存在しない ID は not found・見えない実在 ID は成功」で**存在オラクル**になる。
    """
    hidden = _q("q_sales_secret", DEPT_SENSITIVE, "dept_sales")
    kstore = InMemoryQuestionStore([hidden], embedder)
    ctx = _ctx(term_store, embedder, _steward(), kstore)

    assert kstore.get("q_sales_secret", ctx.viewer) is None, "前提: この viewer には見えない"

    with pytest.raises(ValueError, match="not found or not visible"):
        ctx.set_sfaq_status("q_sales_secret", "contested")

    # 存在オラクルにならないこと — 実在するが見えない ID と、存在しない ID で
    # 返るメッセージが同じ形であること
    with pytest.raises(ValueError, match="not found"):
        ctx.set_sfaq_status("q_totally_made_up", "contested")
    assert kstore.get("q_sales_secret", _steward(("steward", "dept_sales"))) is not None
    assert hidden.status is QuestionStatus.APPROVED, "拒否したのに状態が変わっている"


def test_a_steward_can_still_move_a_question_they_can_read(term_store, embedder):
    """塞ぎすぎていないこと（見える行は動かせる）."""
    visible = _q("q_org", ORG, "org")
    kstore = InMemoryQuestionStore([visible], embedder)
    out = _ctx(term_store, embedder, _steward(), kstore).set_sfaq_status("q_org", "contested")
    assert out == {"question_id": "q_org", "status": "contested"}
