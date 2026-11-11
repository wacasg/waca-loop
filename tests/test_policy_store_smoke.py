"""Smoke test for the Policy store ACL (SPEC_phase1 §9). Offline.

Verifies the two things the policy layer hinges on: the org floor reaches everyone, and dept
rules are viewer-scoped — plus that trial rows ARE served (so they accrue usage) while draft
rows are not.
"""

from __future__ import annotations

from waca_loop.models import Viewer


def test_non_org_member_gets_nothing(policy_store):
    """Policy is internal governance — fail closed for partners/unresolved viewers."""
    outsider = Viewer.anonymous(subject="x")  # no units, not an org member
    assert policy_store.applicable(outsider) == []


def test_org_floor_reaches_every_org_member(policy_store):
    plain_member = Viewer(subject="s", person_id="p", is_org_member=True)  # no dept units
    ids = {r.row_id for r in policy_store.applicable(plain_member)}
    assert {"FLOOR-01", "FLOOR-02"} <= ids     # 配達係: the floor reaches every member
    assert "KYO-06" not in ids                 # dept rule not shown outside the dept


def test_dept_member_gets_floor_plus_dept_rules_incl_trial(policy_store):
    kyozai = Viewer(
        subject="s", person_id="p", is_org_member=True, units=frozenset({"kyozai"})
    )
    ids = {r.row_id for r in policy_store.applicable(kyozai)}
    assert {"FLOOR-01", "FLOOR-02", "KYO-06", "KYO-08"} <= ids  # floor + dept 🟡 + trial (計器盤)
    assert "KYO-09" not in ids                                  # draft is withheld


def test_other_dept_does_not_see_kyozai_rules(policy_store):
    members = Viewer(
        subject="s", person_id="p", is_org_member=True, units=frozenset({"members"})
    )
    ids = {r.row_id for r in policy_store.applicable(members)}
    assert "KYO-06" not in ids and "KYO-08" not in ids
    assert {"FLOOR-01", "FLOOR-02"} <= ids
