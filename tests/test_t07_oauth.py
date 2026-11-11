"""T7 — OAuth: sub取得→persons へマッピング、未マップは actor_raw (§8).

The interactive flow itself is exercised manually (needs Google); here we test the
identity→Viewer resolution and ledger-actor mapping that the flow feeds.
"""

from __future__ import annotations

from waca_loop.auth.identity import ledger_actor, resolve_viewer
from waca_loop.auth.oauth import Identity
from waca_loop.grounding.persons import hash_subject


def test_mapped_subject_resolves_to_person(person_store):
    viewer = resolve_viewer(Identity(subject="sub-sato", email="sato@example.com"), person_store)
    assert viewer.person_id == "p_sato"
    assert viewer.is_org_member is True
    assert "dept_eng" in viewer.units

    actor_id, actor_raw = ledger_actor(viewer)
    assert actor_id == "p_sato"
    assert actor_raw is None


def test_unmapped_subject_falls_back_to_hashed_actor_raw(person_store):
    viewer = resolve_viewer(Identity(subject="sub-unknown"), person_store)
    assert viewer.person_id is None
    assert viewer.is_org_member is False
    assert viewer.subject == hash_subject("sub-unknown")

    actor_id, actor_raw = ledger_actor(viewer)
    assert actor_id is None
    assert actor_raw == hash_subject("sub-unknown")
    assert "sub-unknown" not in actor_raw  # not reversible / not the raw sub
