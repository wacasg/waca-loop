"""§4a — 著者マスク該当行はルーティング原料から除外（SPEC_phase3 §4a・SPEC_phase2 §2.3 E-3(ii)）.

`provenance=corporate_training ∧ perm_corporate ∧ ¬perm_individual` の問いに付いた
フィードバックは、**著者を伏せる約束で配信されている**。これを専門度の材料にすると、
伏せたはずの著者が「その領域の専門家」として名前で出る — 約束が ERN 経由で破れる。

落とすのは**集計前**でなければならない。集計してから候補段で弾いても、その人のシグナルは
他の候補の相対位置（帯の境目）を通じて残る。
"""

from __future__ import annotations

from waca_loop.models import FbType, Provenance


def _masked(ctx, seed, author, times=5):
    """E-3(ii) 該当の問い（法人許諾あり・個人許諾なし）にフィードバックを積む."""
    for _ in range(times):
        seed(ctx, authors=(author,), fb_type=FbType.DECISION,
             provenance=Provenance.CORPORATE_TRAINING,
             perm_corporate=True, perm_individual=False)


def test_a_masked_author_never_becomes_a_candidate(ern_context, ern_seed_fn):
    """C(負): 著者マスク行だけを持つ人は、最強シグナルでも候補にならない."""
    _masked(ern_context, ern_seed_fn, "p_ok", times=10)
    assert ern_context.find_expert("採点基準")["candidates"] == []


def test_the_masked_rows_are_dropped_before_aggregation(ern_context, ern_seed_fn):
    """C(負): 集計前に落ちている — 他の候補の帯に影響していない.

    p_ok2 に弱いシグナル（agree ×1）だけを積む。p_ok のマスク行が集計に入っていれば、
    どこかに痕跡（候補数・帯）が出る。出ないことを確かめる。
    """
    _masked(ern_context, ern_seed_fn, "p_ok", times=10)
    ern_seed_fn(ern_context, authors=("p_ok2",), fb_type=FbType.AGREE)

    cands = ern_context.find_expert("採点基準")["candidates"]
    assert [c["person_id"] for c in cands] == ["p_ok2"]
    assert cands[0]["topic_match"] == "やや近い"


def test_the_same_person_still_counts_through_unmasked_rows(ern_context, ern_seed_fn):
    """A(正): 過剰に落としていない — 同じ人でもマスクでない行は数える.

    「マスクされた問いに関わった人を丸ごと除外する」ではなく、「マスクされた**行**を
    数えない」であること。前者だと、一度でも法人研修に関わった人が永久に候補から消える。
    """
    _masked(ern_context, ern_seed_fn, "p_ok", times=5)
    for _ in range(3):
        ern_seed_fn(ern_context, authors=("p_ok",), fb_type=FbType.DECISION,
                    provenance=Provenance.INDIVIDUAL, perm_individual=True)

    ids = [c["person_id"] for c in ern_context.find_expert("採点基準")["candidates"]]
    assert ids == ["p_ok"]


def test_the_drop_uses_the_shared_mask_author_helper():
    """C(負): 判定を写経していない（`knowledge.acl.mask_author` を呼ぶ）.

    E-3(ii) の条件が2箇所にあると、片方だけ直る。
    """
    import inspect

    from waca_loop.ern import engine

    src = inspect.getsource(engine)
    assert "mask_author" in src
    assert "corporate_training" not in src      # 条件式の写経が無い


def test_routable_feedback_is_a_pure_function():
    """A(正): 原料の絞り込みが純ロジックで、単体で確かめられる."""
    from waca_loop.ern.engine import routable_feedback
    from waca_loop.models import (
        Feedback,
        Question,
        QuestionStatus,
        ScopeAudience,
        ScopeSensitivity,
        ScopeTags,
        ScopeTier,
    )

    scope = ScopeTags(ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL)

    def q(qid, prov, corp, indiv):
        return Question(
            question_id=qid, question_text="採点基準 の決め方", scope=scope,
            provenance=prov, attributes={"domain": ["採点基準"]},
            perm_corporate=corp, perm_individual=indiv, status=QuestionStatus.APPROVED,
        )

    masked = q("q_masked", Provenance.CORPORATE_TRAINING, True, False)
    plain = q("q_plain", Provenance.INDIVIDUAL, False, True)
    fbs = {
        "q_masked": [Feedback(feedback_id="f1", question_id="q_masked",
                              fb_type=FbType.DECISION, author_id="p_ok")],
        "q_plain": [Feedback(feedback_id="f2", question_id="q_plain",
                             fb_type=FbType.DECISION, author_id="p_ok")],
    }
    got = routable_feedback([masked, plain], fbs, "採点基準")
    assert [f.feedback_id for f in got] == ["f2"]


def test_feedback_with_no_author_is_ignored(ern_context, ern_seed_fn):
    """C(負): 組織が著者の行（author_id=None）を誰かの実績にしない.

    過去の記録から移行した判断は `author_id=None` で入る。これを数えると、実在しない専門家が立つ。
    """
    ern_seed_fn(ern_context, authors=(None,), fb_type=FbType.DECISION)
    assert ern_context.find_expert("採点基準")["candidates"] == []
