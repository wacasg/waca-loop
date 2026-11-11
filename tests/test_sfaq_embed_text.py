"""埋め込み対象＝「本文＋見出し」.

表記の揺らぎを正規表現ではなくベクトルで吸収する設計の受け入れテスト。見出し
(question_text) が空振りしても、学びの本文が埋め込まれているので検索に当たる、を表明する。
オフライン (InMemory + HashingEmbedder)。
"""

from __future__ import annotations

import pytest

from waca_loop.embeddings import HashingEmbedder
from waca_loop.knowledge.embed_text import MAX_BODY_CHARS, SEPARATOR, embedding_text
from waca_loop.knowledge.store import InMemoryQuestionStore, new_id
from waca_loop.models import (
    FbType,
    Feedback,
    Provenance,
    Question,
    QuestionStatus,
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Viewer,
)

ORG_SCOPE = ScopeTags(ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL)


def _q(text: str, qid: str | None = None) -> Question:
    return Question(
        question_id=qid or new_id("q"),
        question_text=text,
        scope=ORG_SCOPE,
        provenance=Provenance.CORPORATE_TRAINING,
        perm_corporate=True,   # E-3: 未確認のままだと ACL で落ちる（本テストの主題ではない）
        status=QuestionStatus.PROVISIONAL,
    )


@pytest.fixture
def member() -> Viewer:
    return Viewer(subject="s", person_id="p_m", is_org_member=True,
                  units=frozenset({"members"}))


# --- 純粋関数 ---------------------------------------------------------------

def test_body_comes_first_and_headline_follows():
    out = embedding_text("見出し", "本文である")
    assert out == f"本文である{SEPARATOR}見出し"


def test_missing_body_degrades_to_headline_only():
    """DECISION フィードバックが未着の問いは、見出しだけを埋め込む。"""
    assert embedding_text("見出し", None) == "見出し"
    assert embedding_text("見出し", "") == "見出し"


def test_long_body_is_truncated():
    out = embedding_text("見出し", "あ" * (MAX_BODY_CHARS + 500))
    assert out.startswith("あ" * MAX_BODY_CHARS)
    assert "あ" * (MAX_BODY_CHARS + 1) not in out


# --- InMemory ストア ---------------------------------------------------------

def test_decision_feedback_re_embeds_the_question():
    """本文が後から届いたら埋め込みを作り直す（BQ 側は JOIN するのでこの中間状態が無い）。"""
    store = InMemoryQuestionStore([], HashingEmbedder(dim=64))
    q = _q("— このとき、どう判断するか？", qid="q1")
    store.add_question(q)
    before = list(q.embedding)

    store.add_feedback(Feedback(
        feedback_id="f1", question_id="q1", fb_type=FbType.DECISION,
        body="受講者アンケートは仮名化してから安全チャネルで分析する",
    ))
    assert q.embedding != before, "DECISION 到着後に埋め込みが更新されていない"


def test_non_decision_feedback_does_not_re_embed():
    store = InMemoryQuestionStore([], HashingEmbedder(dim=64))
    q = _q("見出し", qid="q1")
    store.add_question(q)
    before = list(q.embedding)
    store.add_feedback(Feedback(
        feedback_id="f1", question_id="q1", fb_type=FbType.AGREE, body="同意",
    ))
    assert q.embedding == before


def test_body_wording_is_findable_though_the_headline_is_a_dud(member):
    """空振り行（見出しが元の断定文＋接尾辞）でも、本文の語で引ける。

    検索対象を本文まで広げる目的そのもの — 検索の正しさを正規表現の出来から切り離す。
    """
    store = InMemoryQuestionStore([], HashingEmbedder(dim=64))
    dud = _q("受講者アンケートの分析 — このとき、どう判断するか？", qid="q_dud")
    other = _q("請求書の締め日はいつか？", qid="q_other")
    store.add_question(dud)
    store.add_question(other)
    store.add_feedback(Feedback(
        feedback_id="f1", question_id="q_dud", fb_type=FbType.DECISION,
        body="仮名化と集計化を経てから安全チャネルへ入力する",
    ))

    hits = store.search("仮名化と集計化を経てから安全チャネルへ入力する", member, limit=2)
    assert hits, "本文そのもので検索して1件も返らない"
    assert hits[0].question_id == "q_dud"


# --- BigQuery 双子との一致（T10 と同型の SQL 検査） ---------------------------

def test_backfill_sql_embeds_body_and_headline():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from seed_sfaq import _backfill_sql  # noqa: PLC0415

    class _S:
        questions_table = "p.d.questions"
        sfaq_feedback_table = "p.d.feedback"
        embedding_model_ref = "p.d.embedding_model"

    sql = _backfill_sql(_S())
    assert "p.d.feedback" in sql, "本文（feedback）を JOIN していない"
    assert "'decision'" in sql
    assert "q.question_text" in sql
    assert str(MAX_BODY_CHARS) in sql, "切り詰め長が Python 側と食い違っている"
    assert "ARRAY_LENGTH(q.embedding) = 0" in sql

    assert "WHERE TRUE" in _backfill_sql(_S(), regenerate=True)
