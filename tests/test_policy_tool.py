"""get_applicable_policy tool (配達係, SPEC_phase1 §9.2) — offline, via ServerContext directly."""

from __future__ import annotations

from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.models import Viewer
from waca_loop.server import ServerContext


def _ctx(term_store, embedder, viewer, policy_store=None) -> ServerContext:
    return ServerContext(
        term_store=term_store,
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=viewer,
        policy_store=policy_store,
    )


def test_groups_by_level_floor_and_scope(term_store, embedder, policy_store):
    kyozai = Viewer(
        subject="s", person_id="p", is_org_member=True, units=frozenset({"kyozai"})
    )
    out = _ctx(term_store, embedder, kyozai, policy_store).get_applicable_policy()

    assert {r["row_id"] for r in out["level1"]} >= {"KYO-06"}       # dept 条件付き rule
    assert {r["row_id"] for r in out["floor"]} == {"FLOOR-01", "FLOOR-02"}  # bright-lines surfaced
    level2 = {r["row_id"] for r in out["level2"]}
    assert "KYO-08" in level2 and "KYO-09" not in level2            # trial served, draft withheld
    assert out["version"] is not None
    assert "採用レベル" in out["legend"]["level1"]                    # vocabulary rides along
    # floor rows live only in `floor`, never in a level bucket (they carry level NULL)
    assert not any(r["is_floor"] for lvl in ("level2", "level1", "level0") for r in out[lvl])
    # each row carries its 迂回路/条件 (effect) — the guardrail content
    kyo06 = next(r for r in out["level1"] if r["row_id"] == "KYO-06")
    assert "仮名化" in kyo06["effect"] and kyo06["level"] == 1


def test_other_dept_sees_only_floor(term_store, embedder, policy_store):
    members = Viewer(
        subject="s", person_id="p", is_org_member=True, units=frozenset({"members"})
    )
    out = _ctx(term_store, embedder, members, policy_store).get_applicable_policy()
    assert not out["level1"] and not out["level2"]                  # no other dept's rows
    assert {r["row_id"] for r in out["floor"]} == {"FLOOR-01", "FLOOR-02"}


def test_empty_when_policy_unconfigured(term_store, embedder):
    out = _ctx(term_store, embedder, Viewer.anonymous("x")).get_applicable_policy()
    assert out == {
        "level2": [], "level1": [], "level0": [], "floor": [], "legend": {}, "version": None,
    }


def test_non_org_member_gets_empty_policy(term_store, embedder, policy_store):
    """Fail closed: an authenticated partner (is_org_member=False) receives no internal rows —
    and no org vocabulary either (legend stays empty when zero rows are visible)."""
    partner = Viewer(subject="ext", person_id=None, is_org_member=False, is_partner=True)
    out = _ctx(term_store, embedder, partner, policy_store).get_applicable_policy()
    assert not out["level2"] and not out["level1"] and not out["level0"] and not out["floor"]
    assert out["legend"] == {}


def test_unlabeled_business_row_is_delivered_as_level1(term_store, embedder, policy_store):
    """A non-floor row whose level is NULL (hand-edited in BQ past the seeder's validation)
    must not vanish from the response — it lands in level1, matching classify's default
    (§9.2/§9.4 alignment)."""
    from waca_loop.models import PolicyRule

    policy_store.add(PolicyRule(
        row_id="KYO-99", scope_unit="kyozai", level=None,
        line="level未設定の業務行", effect="条件は effect 参照",
    ))
    kyozai = Viewer(
        subject="s", person_id="p", is_org_member=True, units=frozenset({"kyozai"})
    )
    out = _ctx(term_store, embedder, kyozai, policy_store).get_applicable_policy()
    assert "KYO-99" in {r["row_id"] for r in out["level1"]}
    all_ids = {r["row_id"] for lvl in ("level2", "level1", "level0", "floor") for r in out[lvl]}
    assert "KYO-99" in all_ids  # delivered somewhere — never silently dropped
