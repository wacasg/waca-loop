"""ERN のライフサイクル（SPEC_phase3 §5）— frozen の畳み込みと保持期間.

`scripts/purge_frozen_ern.py` は既定 dry-run。削除は明示フラグでしか走らない。
畳み込み（可逆）と削除（不可逆）を別フラグにしてあることも、ここで固定する。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

from waca_loop.ern.lifecycle import RETENTION_DAYS, plan_purge

NOW = datetime(2026, 8, 27, tzinfo=UTC)


def _a(aid, pid, status="declared"):
    return {"assertion_id": aid, "person_id": pid, "status": status}


def _p(status="active", days_ago=0):
    return {"status": status, "updated_at": NOW - timedelta(days=days_ago)}


def test_an_active_persons_assertions_are_untouched():
    """A(正): 在籍中の人の宣言は畳まない."""
    plan = plan_purge([_a("a1", "p1")], {"p1": _p("active")}, now=NOW)
    assert plan.is_empty


def test_a_frozen_persons_assertions_are_folded_to_frozen():
    """A(正): 退職した人の宣言は frozen へ（まず畳む・可逆）."""
    plan = plan_purge([_a("a1", "p1")], {"p1": _p("frozen", days_ago=10)}, now=NOW)
    assert plan.to_freeze == ("a1",)
    assert plan.overdue == ()


def test_a_frozen_assertion_becomes_overdue_after_the_retention_window():
    """A(正)/C(負): 保持期間を過ぎて初めて削除候補になる（境目を固定）."""
    just_under = plan_purge(
        [_a("a1", "p1", "frozen")], {"p1": _p("frozen", days_ago=RETENTION_DAYS - 1)}, now=NOW
    )
    assert just_under.overdue == ()

    at_the_line = plan_purge(
        [_a("a1", "p1", "frozen")], {"p1": _p("frozen", days_ago=RETENTION_DAYS)}, now=NOW
    )
    assert at_the_line.overdue == ("a1",)


def test_orphans_are_reported_separately_from_overdue():
    """C(負): 孤児を「期限切れ」と同じ山に入れない.

    どちらも消す候補だが理由が違う — 孤児は「名簿と宣言がずれている」で、消す前に
    **なぜずれたか**を見るべきもの。まとめると、その問いが消える。
    """
    plan = plan_purge([_a("a1", "ghost")], {}, now=NOW)
    assert plan.orphaned == ("a1",)
    assert plan.overdue == () and plan.to_freeze == ()


def test_a_missing_updated_at_never_becomes_overdue():
    """C(負): 時刻が読めない行を消す候補にしない（fail-closed）."""
    plan = plan_purge(
        [_a("a1", "p1", "frozen")], {"p1": {"status": "frozen", "updated_at": None}}, now=NOW
    )
    assert plan.overdue == ()


def test_the_script_is_dry_run_by_default_and_splits_the_two_applies():
    """C(負): 既定で消さない。畳み込みと削除が別フラグ（可逆と不可逆を混ぜない）."""
    src = (Path(__file__).resolve().parent.parent / "scripts" / "purge_frozen_ern.py").read_text(
        encoding="utf-8"
    )
    assert '"--apply-freeze"' in src
    assert '"--apply-delete"' in src
    assert '"--apply"' not in src, "畳み込みと削除を1つのフラグにまとめない"
    # 削除は apply-delete の下でしか呼ばれない
    delete_at = src.index("DELETE FROM")
    assert "args.apply_delete" in src[:delete_at]


def test_the_weakness_is_written_down_not_hidden():
    """A(正): `frozen_at` を持たない弱点が、コードのそばに書いてある.

    「別要因の更新で時計が延びる」は運用で踏むので、Open item（§7）だと分かる形で
    残っていること。消されていたら落ちる。
    """
    from waca_loop.ern import lifecycle

    doc = inspect.getsource(lifecycle)
    assert "frozen_at" in doc
    assert "updated_at" in doc
