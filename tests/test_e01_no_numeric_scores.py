"""E1 — 出力スキーマに数値評価が無い（INV-5）.

ERN が「評価ではなくマッチング」であることは、思想ではなく**出口の形**で守る。
数が1つ出た時点で順位が読め、組織の中に人を採点する装置ができる。

ここで見るのは2つ: 許可リストの外のキーが無いこと、そして**値に数値が無いこと**。
後者が要る — キー名が無難でも（`topic_match` が 0.87 でも）比較できる数なら順位になる。
"""

from __future__ import annotations

import pytest

from waca_loop.ern.schema import CANDIDATE_KEYS, ScoreLeak, assert_no_scores


def _walk(obj):
    """入れ子を全部たどって値を吐く（戻り値の再帰走査）."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


def test_find_expert_output_has_no_number_anywhere(ern_context, ern_seed_fn):
    """A(正): 実際の応答を再帰走査して、数値が1つも無いこと."""
    ern_seed_fn(ern_context, authors=("p_ok", "p_ok2"))
    out = ern_context.find_expert("採点基準")

    assert out["candidates"], "候補が出ていないと、この検査は空振りになる"
    for value in _walk(out):
        assert not isinstance(value, (int, float)) or isinstance(value, bool), value


def test_candidate_keys_are_exactly_the_allowlist(ern_context, ern_seed_fn):
    """A(正): 候補のキー集合が許可リストに収まる."""
    ern_seed_fn(ern_context)
    for cand in ern_context.find_expert("採点基準")["candidates"]:
        assert set(cand) <= CANDIDATE_KEYS, set(cand) - CANDIDATE_KEYS


@pytest.mark.parametrize(
    "leak",
    [
        {"person_id": "p", "score": 0.9},
        {"person_id": "p", "rank": 1},
        {"person_id": "p", "match_count": 12},
        {"person_id": "p", "topic_match": 0.87},          # キー名は許可・値が数値
        {"person_id": "p", "confidence": "高"},           # 許可リスト外の名前
    ],
)
def test_the_gate_refuses_a_leak(leak):
    """C(負): 数値も、許可リスト外のキーも、関門で落ちる.

    denylist ではなく allowlist にしてあるのが要点 — `confidence` / `weight` / `hits` と
    新しい名前を思いつくたびに追加が要る形にはしない。
    """
    with pytest.raises(ScoreLeak):
        assert_no_scores({"candidates": [leak]})


def test_the_gate_refuses_an_unknown_top_level_key():
    with pytest.raises(ScoreLeak):
        assert_no_scores({"candidates": [], "total": 3})


def test_the_gate_refuses_a_non_string_inside_a_list():
    """C(負): `units` / `declared_topics` に数を紛れ込ませない."""
    with pytest.raises(ScoreLeak):
        assert_no_scores({"candidates": [{"person_id": "p", "units": ["a", 3]}]})


def test_booleans_are_not_treated_as_scores():
    """A(正): bool は int の subclass だが「点数」ではない（過検出で使えなくしない）.

    …とはいえ許可リストに bool を取るキーは今のところ無いので、関門は素通しでよい。
    """
    assert_no_scores({"candidates": []})
