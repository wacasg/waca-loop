"""E9 — 半減期は設定値.

180日が**ロジック内リテラルでない**こと。設定を変えると内部選定が変わる — ただし外部出力に
数値は出さないままなので、**候補集合と帯の変化**で観察する。

なぜ効くか: これは Open item（SPEC_phase3 §7）で、データが貯まってから調整する前提の値。
`engine.py` に `180` と書いた瞬間、設定を変えても候補が変わらなくなり、調整できなくなる。
"""

from __future__ import annotations

import dataclasses
import inspect

from waca_loop.ern.signals import decay
from waca_loop.models import FbType


def test_the_number_180_is_not_written_into_the_logic():
    """C(負): engine / signals / weights に半減期の**数値リテラル**が無い.

    文字列検索ではなく AST の数値定数を見る — 説明文の「180」に反応する検査にすると、
    ルールを説明したコメントを消すインセンティブが生まれる（本末転倒）。
    """
    import ast

    from waca_loop.ern import engine, signals, weights

    for mod in (engine, signals, weights):
        tree = ast.parse(inspect.getsource(mod))
        numbers = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)
        }
        assert 180 not in numbers, f"{mod.__name__} に半減期の数値リテラル"


def test_the_config_carries_it():
    """A(正): 設定から来る（env で上書きできる）."""
    from waca_loop.config import Settings

    assert Settings().ern_half_life_days == 180
    assert Settings(ERN_HALF_LIFE_DAYS=30).ern_half_life_days == 30


def test_decay_halves_at_the_half_life():
    """A(正): 半減期ちょうどで 0.5、2倍で 0.25."""
    assert decay(180, 180) == 0.5
    assert decay(360, 180) == 0.25
    assert decay(0, 180) == 1.0


def test_a_zero_half_life_does_not_crash():
    """C(負): 設定ミス（0 以下）でゼロ除算に落ちない.

    候補が消えるより「全部同じ重み」として現れるほうが気づける。
    """
    assert decay(999, 0) == 1.0
    assert decay(999, -5) == 1.0


def test_changing_the_half_life_changes_the_band(ern_context, ern_seed_fn):
    """A(正): 設定を変えると内部選定が変わる — 帯の変化で観察する.

    古いシグナル（400日前）を1本置く。半減期が長ければ重みが残って「近い」、
    短ければ減衰しきって「やや近い」になる。**出力に数値は出ないまま**変化が読める。
    """
    for _ in range(3):
        ern_seed_fn(ern_context, authors=("p_ok",), fb_type=FbType.DECISION, days_ago=400)

    long_lived = dataclasses.replace(ern_context, ern_half_life_days=3650)
    short_lived = dataclasses.replace(ern_context, ern_half_life_days=30)

    def band(ctx):
        cands = ctx.find_expert("採点基準")["candidates"]
        return cands[0]["topic_match"] if cands else None

    assert band(long_lived) == "近い"
    assert band(short_lived) == "やや近い"


def test_a_short_half_life_can_drop_a_candidate_entirely(ern_context, ern_seed_fn):
    """A(正): 減衰しきれば候補から外れる（集合そのものが変わる）.

    反証（refute）は減衰させても符号が変わらないので、古い弱い肯定 + 新しい反証で
    合計が 0 以下になる状況を作る。
    """
    ern_seed_fn(ern_context, authors=("p_ok",), fb_type=FbType.AGREE, days_ago=400)
    ern_seed_fn(ern_context, authors=("p_ok",), fb_type=FbType.REFUTE, days_ago=1)

    assert dataclasses.replace(ern_context, ern_half_life_days=30).find_expert(
        "採点基準"
    )["candidates"] == []
