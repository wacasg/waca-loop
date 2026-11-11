"""E8 — 逐語なし（INV-1）. T6 と同型の4点検査.

見るのは4箇所: (1) 行モデル (2) 行マッパー/書き込み (3) ツールのシグネチャ (4) DDL。
1箇所でも逐語の器があると、そこへ流し込む経路が後から生える。
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

from waca_loop.ern.store import ErnAssertion, ErnAuditRow

DDL = Path(__file__).resolve().parent.parent / "sql" / "ddl" / "15_ern.sql"

#: 逐語を持ち込む列/フィールド名。`body` は SFAQ 側では正当だが、ERN には要らない。
VERBATIM = ("prompt", "raw_text", "verbatim", "transcript", "body", "content",
            "message", "query_text", "note")


def test_1_the_row_models_have_no_verbatim_field():
    for cls in (ErnAssertion, ErnAuditRow):
        names = {f.name.lower() for f in dataclasses.fields(cls)}
        assert not (names & set(VERBATIM)), (cls.__name__, names & set(VERBATIM))


def test_2_the_write_path_never_sends_a_verbatim_key():
    from waca_loop.ern import store

    src = inspect.getsource(store)
    for bad in VERBATIM:
        assert f'"{bad}"' not in src, f"書き込み経路に逐語の列名: {bad}"


def test_3_the_tool_signature_takes_a_topic_not_a_question():
    """C(負): `find_expert` が受けるのは領域ラベルであって、利用者の文ではない.

    `topic` は正規化済みのラベルとして扱う。ここが `question` / `prompt` だと、
    監査行に利用者の書いた文が丸ごと残る器ができる。
    """
    from waca_loop.server import ServerContext

    params = set(inspect.signature(ServerContext.find_expert).parameters)
    assert params == {"self", "topic", "k"}


def test_4_the_ddl_has_no_verbatim_column():
    sql = DDL.read_text(encoding="utf-8")
    for line in sql.splitlines():
        code = line.split("--", 1)[0].strip()
        if not code:
            continue
        name = code.split()[0].lower().strip("(),`")
        assert name not in VERBATIM, line


def test_the_audit_row_records_the_topic_not_the_prose(ern_context, ern_seed_fn):
    """A(正): 監査に残るのは領域ラベルだけ（候補の本文もフィードバック本文も残らない）."""
    ern_seed_fn(ern_context, authors=("p_ok",))
    ern_context.find_expert("採点基準")

    row = ern_context.ern_audit_store.rows[0]
    assert row.topic == "採点基準"
    assert "そう決めた" not in str(row)      # フィードバック本文


# --- 5点目: 呼び出し側の文字列が表に書かれないこと -------------------------------------------

def test_5_a_sentence_passed_as_topic_is_not_persisted(ern_context, ern_seed_fn):
    """C(負): `find_expert` に文を渡しても、監査行に本文が残らない.

    上の4点検査は**名前**（列名・フィールド名・引数名）しか見ないので、`topic` という
    無難な名前の列に自由文が入る形は捕まえられない。DDL の「問い合わせ本文ではない」を
    守らせるのはこのテストである。

    実際に来うる形:

        find_expert("田中部長のパワハラ相談を誰にすればいいか、人事の懲戒手続に詳しい人")

    これがそのまま `<DS_FEEDBACK>.ern_audit.topic` に**永久に**残ると、人名・相談の内容を
    append-only の表に書いたことになる（`purge_frozen_ern.py` はこの表を触らない）。
    """
    sentence = "田中部長のパワハラ相談を誰にすればいいか、人事の懲戒手続に詳しい人"
    ern_seed_fn(ern_context, authors=("p_ok",))
    ern_context.find_expert(sentence)

    rows = ern_context.ern_audit_store.rows
    assert len(rows) == 1, "検索そのものは記録される（探されたことは残る）"
    assert rows[0].topic is None, "文は残さない"
    assert "田中" not in str(rows[0])


def test_5_a_short_label_is_still_recorded(ern_context, ern_seed_fn):
    """A(正): ラベルなら残す — 監査を無意味にしていない（過剰に落としていない）."""
    ern_seed_fn(ern_context, authors=("p_ok",))
    ern_context.find_expert("採点基準")
    assert ern_context.ern_audit_store.rows[0].topic == "採点基準"


def test_5_the_bound_drops_rather_than_truncates():
    """C(負): 長すぎるものを**切り詰めない**.

    途中で切ると、断片が残るうえに「これはラベルである」という嘘が付く。
    """
    from waca_loop.ern.engine import TOPIC_LABEL_MAX, audit_topic

    assert audit_topic("あ" * TOPIC_LABEL_MAX) == "あ" * TOPIC_LABEL_MAX
    assert audit_topic("あ" * (TOPIC_LABEL_MAX + 1)) is None
    assert audit_topic("   ") is None


def test_5_sentence_punctuation_is_rejected_regardless_of_length():
    """C(負): 短くても、句読点を含めば文として落とす.

    長さだけでは足りない — 24 字以内で人名と用件を書いた文はいくらでも作れる。
    用語に `、。？?！!` を含むものはまず無いので、これは実在の語彙を落とさずに文だけを
    弾ける。
    """
    from waca_loop.ern.engine import audit_topic

    for sentence in ("田中さんの件、誰に聞く？", "解雇手続に詳しい人は？", "至急！人事の人"):
        assert audit_topic(sentence) is None, sentence

    # 用語らしい語は落とさない（空白入りの英語語も通す）
    for label in ("採点基準", "Human in the Loop", "委員会A推進部会",
                  "ワークスペース / プロジェクト", "Prompt Injection Attack"):
        assert audit_topic(label) is not None, label


def test_5_the_search_itself_still_works_with_a_long_topic(ern_context, ern_seed_fn):
    """A(正): 監査に残さないだけで、検索は普通に動く（機能を壊していない）."""
    ern_seed_fn(ern_context, authors=("p_ok",), topic="採点基準")
    long_topic = "採点基準" + "。" * 60
    out = ern_context.find_expert(long_topic)
    assert isinstance(out["candidates"], list)
