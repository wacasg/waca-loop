"""use_category suggestion + floor warnings from declared ``data_kinds`` (§9.4).

Pure logic — no BigQuery imports. A row participates only when its human-authored
``trigger_kinds`` intersects the declared ``data_kinds`` (配達係: humans write the
triggers, WACA Loop only reads them). Advisory by design: the caller records the usage
regardless — 統治の負荷はリスクに比例, warn and show the detour, never block.

Policy rows carry 採用レベル ``level`` + ``is_floor``, and the *ledger* axis
``use_category`` speaks the same canon — **L0/L1/L2 ＋ 禁止列挙** — so a fired row maps
onto it directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from waca_loop.models import PolicyRule, UseCategory

# Severity order for suggestion/mismatch comparisons (禁止列挙 is heaviest).
_SEVERITY = {
    UseCategory.L2: 0,
    UseCategory.L1: 1,
    UseCategory.L0: 2,
    UseCategory.PROHIBITED: 3,
}
_BY_SEVERITY = [UseCategory.L2, UseCategory.L1, UseCategory.L0, UseCategory.PROHIBITED]


def _rule_severity(rule: PolicyRule) -> int:
    """Map a policy row onto the ledger's L0/L1/L2＋禁止列挙 severity scale (§9.4).

    下限 (``is_floor``) is its own heaviest bucket (``prohibited``), separate from level 0.
    Level 0 (未使用 — the org has decided not to use AI here) sits just below;
    level 1 (条件付き) in the middle; level 2 (そのまま採用) is lightest. An unlabeled
    business row defaults to the middle — conservative without crying wolf.
    """
    if rule.is_floor:
        return 3
    if rule.level == 0:
        return 2
    if rule.level == 2:
        return 0
    return 1


@dataclass
class Classification:
    """Advisory result served back in the ``log_usage`` response (§9.4)."""

    # heaviest 利用区分 among fired rows; None = no row fired
    suggested_category: UseCategory | None = None
    mismatch: bool = False                      # declared category lighter than the suggestion
    floor_warnings: list[dict] = field(default_factory=list)  # fired 下限 rows, with detours

    def to_tool_output(self) -> dict:
        return {
            "suggested_category": (
                self.suggested_category.value if self.suggested_category else None
            ),
            "mismatch": self.mismatch,
            "floor_warnings": list(self.floor_warnings),
        }


def classify_usage(
    data_kinds: list[str],
    declared_category: UseCategory,
    rules: list[PolicyRule],
) -> Classification:
    """Suggest a 利用区分 and surface fired 下限 rows for one usage (§9.4).

    ``rules`` must already be ACL-filtered for the viewer (the same double gate as
    ``get_applicable_policy``) so a dept's triggers never leak across departments.
    """
    declared = set(data_kinds)
    fired = [r for r in rules if r.trigger_kinds and declared & set(r.trigger_kinds)]
    if not fired:
        return Classification()

    suggested = _BY_SEVERITY[max(_rule_severity(r) for r in fired)]
    floors = [
        {"row_id": r.row_id, "line": r.line, "detour": r.effect}
        for r in fired
        if r.is_floor
    ]
    return Classification(
        suggested_category=suggested,
        mismatch=_SEVERITY[declared_category] < _SEVERITY[suggested],
        floor_warnings=floors,
    )
