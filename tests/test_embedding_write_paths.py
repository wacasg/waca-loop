"""埋め込みを書く2つの経路が、同じ文字列を同じ列へ書くことを固定する（W3・SQL 契約）.

質問ベクトルを書く経路は2つある — 到着時の `BigQueryQuestionStore._reembed` と、まとめ直す
`scripts/seed_sfaq.py --backfill-embeddings`。**どちらもオフラインでは1行も実行されない**
（docs/TEST_STRATEGY.md §1）。この2経路で起きうる欠陥は3つ:

- 到着時に再埋め込みしないと、オフラインは緑・本番だけ黙って劣化する
- `_reembed` が「いま届いた本文」を埋め込むと、正典は**最初の DECISION** なので、2件目が
  届くたびに本番のベクトルだけ正典から外れ、次の backfill で戻る
- backfill SQL が全体だけ TRIM すると、Python 側（要素ごとに strip）と別の文字列を
  埋め込む。さらに UPDATE が `updated_at` を触らないと、
  「再生成が走っていない」という**誤検知**の材料になる

いずれも「拒否されること」ではなく「何を書いたか」の話なので、偽クライアントで
組み立てられた SQL とパラメータを見る。`test_sfaq_embed_text.py` は正典側
（`embedding_text`）の形を、`test_twin_conformance.py` は双子の面を見ており、ここは重複しない。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class _Job:
    def __init__(self, rows):
        self._rows = rows
        self.num_dml_affected_rows = 1

    def result(self):
        return iter(self._rows)


class _ScriptedClient:
    """SELECT には仕込んだ行を返し、投げられた SQL とパラメータを記録する偽クライアント。"""

    def __init__(self, question_text: str, decisions: list[str]):
        self._question_text = question_text
        self._decisions = decisions
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, job_config=None):
        params = {p.name: p.value for p in (getattr(job_config, "query_parameters", None) or [])}
        self.calls.append((sql, params))
        if "SELECT question_text" in sql:
            return _Job([{"question_text": self._question_text}])
        if "SELECT body" in sql:
            return _Job([{"body": b} for b in self._decisions[:1]])
        return _Job([])


class _StubEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.0] * 8


class _Settings:
    gcp_project = "p"
    questions_table = "p.d.questions"
    sfaq_feedback_table = "p.d.feedback"
    embedding_model_ref = "p.d.embedding_model"
    embedding_provider = "bq_ml"
    embedding_dim = 8
    terms_table = "p.d.terms"
    persons_table = "p.d.persons"


_VIEWER = None   # `_reembed` は viewer を使わない（ACL を通らない読み口・下の不変条件を参照）


def _reembed_call(question_text: str, decisions: list[str], arriving: str):
    from waca_loop.knowledge.store import BigQueryQuestionStore

    client = _ScriptedClient(question_text, decisions)
    store = BigQueryQuestionStore(client, _Settings(), _VIEWER, _StubEmbedder())
    store._reembed("q1", arriving)
    update = next((sql, prm) for sql, prm in client.calls if sql.lstrip().startswith("UPDATE"))
    return client, update


def test_the_first_decision_is_what_gets_embedded():
    """2件目の DECISION が届いても、埋め込む本文は最初の DECISION のまま。

    naive 版（届いた本文をそのまま使う）は他のどのテストも落とさない — 本番のベクトルだけが
    静かに揺れる種類の欠陥だからである。
    """
    from waca_loop.knowledge.embed_text import embedding_text

    first, later = "最初に下した判断の本文", "あとから足された二件目の判断"
    _, (sql, params) = _reembed_call("見出し", [first], later)

    assert params["content"] == embedding_text("見出し", first)
    assert later not in params["content"], "いま届いた本文で正典を上書きしている"
    assert "ORDER BY created_at LIMIT 1" in "".join(s for s, _ in _reembed_call(
        "見出し", [first], later)[0].calls), "最初の DECISION を選んでいない"
    assert "@content" in sql, "本文を SQL に直書きしている（パラメータで渡すこと）"
    assert "updated_at = CURRENT_TIMESTAMP()" in sql


def test_a_question_without_a_decision_falls_back_to_the_arriving_body():
    """まだ読めない（streaming buffer）ときだけ、いま届いた本文で代用する。"""
    from waca_loop.knowledge.embed_text import embedding_text

    _, (_, params) = _reembed_call("見出し", [], "いま届いた本文")
    assert params["content"] == embedding_text("見出し", "いま届いた本文")


def test_reembed_never_hands_the_body_back_to_the_caller():
    """ACL を通らない2つ目の読み口なので、本文の出口を作らない."""
    from waca_loop.knowledge.store import BigQueryQuestionStore

    client = _ScriptedClient("見出し", ["秘密の判断本文"])
    store = BigQueryQuestionStore(client, _Settings(), _VIEWER, _StubEmbedder())
    assert store._reembed("q1", "x") is None


def test_backfill_trims_the_body_and_the_headline_separately():
    """全体だけ TRIM すると Python 側と別の文字列になる.

    `embedding_text` は本文と見出しを**要素ごとに** strip する。SQL 側が
    `TRIM(CONCAT(...))` だけだと、本文末尾の空白が区切りの手前に残って別の文字列になる。
    naive 版（外側 TRIM のみ）は既存の backfill テストを1つも落とさない。
    """
    from seed_sfaq import _backfill_sql  # noqa: PLC0415

    sql = _backfill_sql(_Settings())
    assert "TRIM(SUBSTR(f.body" in sql, "本文を個別に TRIM していない"
    assert "TRIM(q.question_text)" in sql, "見出しを個別に TRIM していない"


def test_backfill_marks_the_rows_it_rewrote():
    """`updated_at` を更新しないと「再生成が走っていない」と読める."""
    from seed_sfaq import _backfill_sql  # noqa: PLC0415

    sql = _backfill_sql(_Settings())
    update = sql[sql.index("UPDATE `p.d.questions`"):]
    assert "updated_at = CURRENT_TIMESTAMP()" in update


@pytest.mark.parametrize("regenerate", [False, True])
def test_backfill_and_arrival_embed_the_same_two_parts(regenerate):
    """2経路が同じ材料（最初の DECISION 本文 ＋ 見出し）を見ていることの表明（W2-6）."""
    from seed_sfaq import _backfill_sql  # noqa: PLC0415

    sql = _backfill_sql(_Settings(), regenerate=regenerate)
    assert "fb_type = 'decision'" in sql
    assert "ARRAY_AGG(body ORDER BY created_at LIMIT 1)" in sql, "最初の DECISION でない"
