"""E10 — 根拠種別の明示.

シグナルゼロの領域では役職ベースのフォールバックが働き `evidence_type="役職ベース"`、
シグナルがあれば `"実績ベース"`。**混在時も候補ごとに正しく付く**。

なぜ要るか: 「その人が詳しい」と「その人がその役職にいる」は根拠の強さが違う。
区別せずに3名並べると、聞く側は全員が実績のある人だと読む。
"""

from __future__ import annotations

import dataclasses

from waca_loop.ern.weights import EVIDENCE_ROLE, EVIDENCE_SIGNAL
from waca_loop.models import FbType


def test_signal_backed_candidates_say_so(ern_context, ern_seed_fn):
    """A(正): シグナルがあれば実績ベース."""
    ern_seed_fn(ern_context, authors=("p_ok",), fb_type=FbType.DECISION)
    cand = ern_context.find_expert("採点基準")["candidates"][0]
    assert cand["evidence_type"] == EVIDENCE_SIGNAL


def test_cold_start_falls_back_to_role(ern_context):
    """A(正): シグナルゼロの領域では役職ベースで返る（誰にも聞けない、にしない）."""
    ctx = dataclasses.replace(
        ern_context, ern_role_fallback={"新しい領域": ("p_ok2",)}
    )
    cands = ctx.find_expert("新しい領域")["candidates"]
    assert [c["person_id"] for c in cands] == ["p_ok2"]
    assert cands[0]["evidence_type"] == EVIDENCE_ROLE


def test_a_mixed_result_labels_each_candidate_correctly(ern_context, ern_seed_fn):
    """A(正): 実績で埋まったぶんは実績、足りないぶんだけ役職で補う."""
    ern_seed_fn(ern_context, authors=("p_ok",), fb_type=FbType.DECISION)
    ctx = dataclasses.replace(ern_context, ern_role_fallback={"採点基準": ("p_ok2",)})

    by_id = {c["person_id"]: c for c in ctx.find_expert("採点基準")["candidates"]}
    assert by_id["p_ok"]["evidence_type"] == EVIDENCE_SIGNAL
    assert by_id["p_ok2"]["evidence_type"] == EVIDENCE_ROLE


def test_the_fallback_never_duplicates_someone_already_picked(ern_context, ern_seed_fn):
    """C(負): 実績で出た人が役職側でも出て二重にならない."""
    ern_seed_fn(ern_context, authors=("p_ok",), fb_type=FbType.DECISION)
    ctx = dataclasses.replace(ern_context, ern_role_fallback={"採点基準": ("p_ok", "p_ok2")})

    ids = [c["person_id"] for c in ctx.find_expert("採点基準")["candidates"]]
    assert ids == sorted(set(ids))
    assert ids.count("p_ok") == 1


def test_the_fallback_still_passes_the_consent_gate(ern_context):
    """C(負): 役職ベースでも、未同意者・退職者は出ない（フォールバックが抜け道にならない）."""
    ctx = dataclasses.replace(
        ern_context,
        ern_role_fallback={"新しい領域": ("p_optout", "p_frozen", "p_ok")},
    )
    ids = [c["person_id"] for c in ctx.find_expert("新しい領域")["candidates"]]
    assert ids == ["p_ok"]


def test_the_fallback_uses_the_weak_bands(ern_context):
    """A(正): 役職ベースの帯は実績のそれと取り違えられない側に固定."""
    ctx = dataclasses.replace(ern_context, ern_role_fallback={"新しい領域": ("p_ok",)})
    cand = ctx.find_expert("新しい領域")["candidates"][0]
    assert cand["topic_match"] == "やや近い"
    assert cand["freshness"] == "しばらく前"
