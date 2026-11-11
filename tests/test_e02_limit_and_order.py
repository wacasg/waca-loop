"""E2 — 上限3名・順不同（SPEC_phase3 §3）.

「良い順」を構造で壊す。数値を出していなくても、**並びから順位が読めれば実質ランキング**
になる。だから内部では専門度で選び、返す前に person_id で並べ直す。
"""

from __future__ import annotations

from waca_loop.models import FbType
from waca_loop.server import _K_CAPS


def test_the_cap_is_three_and_lives_in_the_shared_clamp():
    """A(正): 上限は `_K_CAPS` にあり、ERN 独自の上限機構ではない（§0 前提）."""
    assert _K_CAPS["find_expert"] == 3


def test_never_more_than_three_even_when_more_people_qualify(ern_context, ern_seed_fn,
                                                            ern_persons):
    """A(正): 資格のある人が4人いても3名まで."""
    from waca_loop.models import Person

    extra = [
        Person(person_id=f"p_x{i}", display_name=f"追加 {i}",
               unit_path=["org"], consent_internal=True)
        for i in range(4)
    ]
    for p in extra:
        ern_context.person_store._persons[p.person_id] = p   # noqa: SLF001

    ern_seed_fn(ern_context, authors=tuple(p.person_id for p in extra) + ("p_ok", "p_ok2"))
    assert len(ern_context.find_expert("採点基準")["candidates"]) == 3


def test_a_larger_k_is_clamped_not_honoured(ern_context, ern_seed_fn):
    """C(負): k を大きくしても3名を超えない（クランプは `_clamp_k` が行う）."""
    ern_seed_fn(ern_context, authors=("p_ok", "p_ok2"))
    assert len(ern_context.find_expert("採点基準", k=100)["candidates"]) <= 3


def test_the_order_is_person_id_ascending_not_strength(ern_context, ern_seed_fn):
    """C(負): 並びが「良い順」になっていない.

    p_ok2 に強いシグナル（decision ×3）、p_ok に弱いシグナル（agree ×1）を積む。
    内部選定では p_ok2 が先だが、**返りは person_id 昇順**なので p_ok が先に来る。
    ここが選定順のまま返っていたら落ちる。
    """
    for _ in range(3):
        ern_seed_fn(ern_context, authors=("p_ok2",), fb_type=FbType.DECISION)
    ern_seed_fn(ern_context, authors=("p_ok",), fb_type=FbType.AGREE)

    ids = [c["person_id"] for c in ern_context.find_expert("採点基準")["candidates"]]
    assert ids == sorted(ids)
    assert ids[0] == "p_ok"      # 弱いほうが先 = 強さ順で並んでいない


def test_the_band_still_reflects_strength_even_though_the_order_does_not(ern_context,
                                                                        ern_seed_fn):
    """A(正): 順位は消すが、帯は残す（「近い/やや近い」は判断の役に立つ情報）."""
    for _ in range(3):
        ern_seed_fn(ern_context, authors=("p_ok2",), fb_type=FbType.DECISION)
    ern_seed_fn(ern_context, authors=("p_ok",), fb_type=FbType.AGREE)

    by_id = {c["person_id"]: c for c in ern_context.find_expert("採点基準")["candidates"]}
    assert by_id["p_ok2"]["topic_match"] == "近い"
    assert by_id["p_ok"]["topic_match"] == "やや近い"
