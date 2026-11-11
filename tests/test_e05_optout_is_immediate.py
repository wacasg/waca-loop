"""E5 — オプトアウト即時反映（INV-7・SPEC_phase3 §4c）.

同意を降ろした直後の呼び出しで消えること。**キャッシュを置かない**のがこの一線で、
「次のバッチまで残る」は「本人が降ろしたのに出続けた時間があった」と同義である。
"""

from __future__ import annotations

from waca_loop.models import FbType


def _seed_strong(ctx, seed, person_id="p_ok", times=5):
    for _ in range(times):
        seed(ctx, authors=(person_id,), fb_type=FbType.DECISION)


def test_flipping_consent_removes_the_person_on_the_very_next_call(ern_context, ern_seed_fn):
    """A(正)/C(負): 1回目に出た人が、同意を降ろした直後の2回目で消える."""
    _seed_strong(ern_context, ern_seed_fn)

    first = [c["person_id"] for c in ern_context.find_expert("採点基準")["candidates"]]
    assert "p_ok" in first

    ern_context.person_store._persons["p_ok"].consent_internal = False   # noqa: SLF001

    second = [c["person_id"] for c in ern_context.find_expert("採点基準")["candidates"]]
    assert "p_ok" not in second


def test_freezing_a_person_removes_them_on_the_very_next_call(ern_context, ern_seed_fn):
    """A(正): 退職（frozen）も同じく即時."""
    _seed_strong(ern_context, ern_seed_fn)
    assert ern_context.find_expert("採点基準")["candidates"]

    ern_context.person_store._persons["p_ok"].status = "frozen"          # noqa: SLF001
    assert ern_context.find_expert("採点基準")["candidates"] == []


def test_the_engine_holds_no_candidate_cache():
    """C(負): エンジンに候補のキャッシュ属性が無い（構造で担保する）.

    振る舞い側（上の2本）だけだと、TTL 付きキャッシュを入れても「テストが速すぎて」
    緑のまま通る余地がある。**キャッシュを置かない**という設計そのものを表明しておく。
    """
    import inspect

    from waca_loop.ern import engine

    src = inspect.getsource(engine)
    for bad in ("lru_cache", "cached_property", "_cache", "functools.cache"):
        assert bad not in src, f"ERN のエンジンに {bad} がある — opt-out が即時に効かなくなる"


def test_the_disclosure_gate_is_consulted_per_candidate(ern_context, ern_seed_fn):
    """A(正): 同意の判定が呼び出しごとに引き直されている.

    `person_visible_to` の呼び出し回数を数える。0 回なら、どこかに結果が溜まっている。
    """
    from waca_loop.ern import engine

    calls = []
    original = engine.person_disclosable_as_expert

    def counting(person, viewer):
        calls.append(person.person_id)
        return original(person, viewer)

    engine.person_disclosable_as_expert = counting
    try:
        _seed_strong(ern_context, ern_seed_fn)
        ern_context.find_expert("採点基準")
        first = len(calls)
        ern_context.find_expert("採点基準")
        assert len(calls) > first, "2回目が判定を引き直していない（キャッシュの疑い）"
    finally:
        engine.person_disclosable_as_expert = original
