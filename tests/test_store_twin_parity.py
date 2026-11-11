"""双子ストアの公開メソッド集合が一致することを表明する.

たとえば `roster_names()` を InMemory 側にだけ足すと、本番の `ServerContext.person_store` は
BigQuery 側なので、`raise_lesson_from_task` は本番でだけ AttributeError で落ちる。
オフラインテストは双子側しか踏まないので**永久に緑**のままである。

「BigQuery 側だけが違う・オフラインでは再現できない」は起きやすい形なので、
**メソッドの生やし忘れ**だけでも機械的に落ちるようにしておく。
"""

from __future__ import annotations

import pytest

from waca_loop.grounding.persons import BigQueryPersonStore, InMemoryPersonStore
from waca_loop.grounding.terms import BigQueryTermStore, InMemoryTermStore
from waca_loop.knowledge.store import BigQueryQuestionStore, InMemoryQuestionStore
from waca_loop.ledger.usage_log import BigQueryUsageLogStore, InMemoryUsageLogStore


def _public(cls: type) -> set[str]:
    return {n for n in dir(cls) if not n.startswith("_")}


# 片方にしか無いことが**意図されている**メソッドはここに書く。書くこと自体が
# 「なぜ非対称か」を残す場になる。
KNOWN_ASYMMETRY: dict[tuple[type, type], set[str]] = {
    # BigQueryTermStore に add は無い（用語の投入は seed スクリプト経由・§2.2）。
    (InMemoryTermStore, BigQueryTermStore): {"add"},
}


@pytest.mark.parametrize(
    ("in_memory", "bigquery"),
    [
        (InMemoryPersonStore, BigQueryPersonStore),
        (InMemoryTermStore, BigQueryTermStore),
        (InMemoryQuestionStore, BigQueryQuestionStore),
        (InMemoryUsageLogStore, BigQueryUsageLogStore),
    ],
)
def test_the_twins_expose_the_same_surface(in_memory, bigquery):
    allowed = KNOWN_ASYMMETRY.get((in_memory, bigquery), set())
    only_in_memory = _public(in_memory) - _public(bigquery) - allowed
    only_bigquery = _public(bigquery) - _public(in_memory) - allowed
    assert not only_in_memory, (
        f"{in_memory.__name__} にしか無い: {sorted(only_in_memory)} — "
        "本番は BigQuery 側なので、ここが空でないとオフライン緑のまま本番で落ちる"
    )
    assert not only_bigquery, f"{bigquery.__name__} にしか無い: {sorted(only_bigquery)}"


def test_the_roster_gate_exists_on_the_production_store():
    """氏名ゲートは本番側にあってこそ意味がある."""
    assert hasattr(BigQueryPersonStore, "roster_names")


def test_the_roster_query_asks_only_for_names_of_active_people():
    """名簿は**絞らない**（絞ると検査が甘くなる）が、退職者と NULL は外す."""
    pytest.importorskip("google.cloud.bigquery")

    class _Job:
        def result(self):
            return iter([{"display_name": "山田 太郎"}])

    class _Client:
        def __init__(self):
            self.sql = ""

        def query(self, sql, job_config=None):
            self.sql = sql
            return _Job()

    class _Settings:
        persons_table = "p.d.persons"

    client = _Client()
    got = BigQueryPersonStore(client, _Settings()).roster_names()
    assert got == ("山田 太郎",)
    assert "display_name" in client.sql
    assert "status = 'active'" in client.sql
    # consent や circles で絞らないこと — 名簿を狭めると検査が漏れる側に倒れる
    assert "consent" not in client.sql
    assert "circles" not in client.sql


def test_candidate_payload_survives_double_encoding():
    """BigQuery の JSON 列は二重エンコードされて返ることがある.

    1回しか解かないと payload が str のままで、承認が本番でだけ落ちる。
    """
    import json

    from waca_loop.improvement.candidates import _decode_payload

    want = {"term": "サン番", "definition": "会員番号"}
    assert _decode_payload(want) == want
    assert _decode_payload(json.dumps(want, ensure_ascii=False)) == want
    assert _decode_payload(json.dumps(json.dumps(want, ensure_ascii=False))) == want
    # 解けないものは落とす。`{}` を返すと、壊れた候補が「中身の無い正当な候補」に化け、
    # TERM 承認が空の用語を共有語彙へ書き込む。
    for bad in (None, "not json", "[]", '"bare string"', 3):
        with pytest.raises(ValueError):
            _decode_payload(bad)
