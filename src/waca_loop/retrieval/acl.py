"""ACL — 権限を引き継ぐ検索 (§3.3). The most security-critical module.

Two enforcement points, by design (§3.3 "二重で効かせる"):
  1. :func:`visible_to` / :func:`acl_filter` — in-process post-filter on results.
  2. :func:`acl_where_clause` — a SQL WHERE fragment so forbidden rows never leave BQ.

Both implement the SAME rules. A row is visible only if ALL THREE axes pass.
"""

from __future__ import annotations

from waca_loop.models import (
    Person,
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    TermHit,
    Viewer,
)
from waca_loop.org import get_org_config

# The unit ids below are the adopting organization's own (org.toml [units], see
# waca_loop/org.py); the comments use the neutral defaults `org` / `steward` / `board` / `owner`.
_UNITS = get_org_config().units

#: The unit every member carries (org.toml [units] org).
ORG_UNIT = _UNITS.org

# Units that grant no *ownership*. `org` is appended to EVERY person's unit_path
# (scripts/sync_persons_from_sheet.py), so a record owned by it is org-visible while looking
# scoped, and "shares a unit with me" would be true of the whole organization.
#
# Deliberately NOT including `steward`: it is a small role group, not org-wide reach.
# A record owned by it is visible to the stewards — which is a real, narrow audience — and
# excluding it would leave a steward-only member unable to file anything at all.
#
# ONE consumer: `owning_units` → `resolve_owner_unit` (the WRITE side — where a new record
# may be filed). Nothing on the read side uses it.
#
# 注意: `person_visible_to` / `person_where_clause`（read side）は **units にも ORG_WIDE_UNITS
# にも触れない**（名簿のゲートは同意・在籍・メンバー資格で決まり、unit 照合をしない）。
# 消費者はこのファイルの `owning_units` 1箇所だけである。
#
# その帰結を書いておく: 読み側では罠が**生きている**。`owner_unit='org'` の
# dept 行は `owner_unit in viewer.units` が全員で真になり、org tier と同じ範囲へ届く —
# スコープが付いているのに実質全社公開。record の読みゲート
# (`visible_to` / `acl_where_clause`) がこれを参照しないのは意図的で、org 相当を指したい
# ときは **tier を org にする**のが正しいやり方（`docs/ACCESS_MATRIX.md` §0 落とし穴2）。
ORG_WIDE_UNITS = frozenset({ORG_UNIT})

# The governance role group (SPEC_phase2 §2.4 承認は人＝スチュワード). A role slug baked into
# the spec, not a deployment knob — but it belongs in ONE place, since it gates every
# promotion/status transition and is where an un-routable incident report lands.
STEWARD_UNIT = _UNITS.steward

# 理事会 (§3.3). Unlike every other unit this one is named in code, because it is the one whose
# decisions routinely CANNOT be shared org-wide — personnel, contracts, disputes. Opening
# existing knowledge to everyone is right for vocabulary and curricula and wrong for a board
# minute. Because opening is the easy path, the closing has to be structural rather than
# remembered: `widen_refusal` refuses to put a board-owned row on the org
# tier at all. A default that publishes needs an origin that cannot be published by mistake.
BOARD_UNIT = _UNITS.board

# 改善要望の「見送り」を決められる役割ユニット (SPEC_improvement_requests §8)。
# WACA Loop の管理者だけに付ける。**スチュワード全員ではない** — ほかの状態はどれも
# 途中経過の記録で、間違えれば次の更新で直る。wontfix だけは「直さないと決めた」結論で、
# 起案者への説明責任が伴い、放っておくとその要望は二度と提示に出てこない。
# 記録の権限と、打ち切る権限は別物である。
#
# 組織シートが表現できないユニットなので、`sync_persons_from_sheet.manual_units` の
# carry-forward で再同期を生き延びる（steward / export_admin と同じ扱い）。
OWNER_UNIT = _UNITS.owner


def widen_refusal(owner_unit: str | None, sensitivity: ScopeSensitivity) -> str | None:
    """Why this row may NOT be widened to the org tier, or None if it may (§3.3).

    Returns a human-readable reason so the caller can tell the steward what stopped it —
    a silent refusal in a governance tool is indistinguishable from a bug.
    """
    if owner_unit == BOARD_UNIT:
        return (
            f"理事会({BOARD_UNIT})所有の判断は全社公開にできない。"
            "公開してよい内容なら、所有ユニットを実務側の単位に付け替えてから広げること"
        )
    if sensitivity is ScopeSensitivity.SENSITIVE:
        return "機微(sensitive)の行は全社公開にできない。先に sensitivity を下げること"
    return None


def owning_units(viewer: Viewer) -> frozenset[str]:
    """The viewer's units that can *own* a record (org-wide/role units removed, §3.3)."""
    return frozenset(viewer.units) - ORG_WIDE_UNITS


def resolve_owner_unit(viewer: Viewer, requested: str | None = None) -> str:
    """The unit a record filed by ``viewer`` belongs to (§3.3). Fails closed, never guesses.

    ``Viewer.units`` is an unordered frozenset built from ``person.unit_path``
    (auth/identity.py), so leaf→ancestor order is already lost by the time we get here:
    there is no way to recover which unit is the filer's "home" one. Guessing (hash order,
    or alphabetical) would file a multi-unit member's record into an arbitrary unit — for a
    member of both a department and a circle, plausibly the circle, which is invisible to
    the department that actually owns the judgment.

    So: one owning unit → use it. Several → the caller must name one (``requested``), and it
    must be a unit the viewer actually belongs to. None → refuse. Every branch raises rather
    than falling back to org-wide visibility.
    """
    units = owning_units(viewer)
    if requested is not None:
        if requested not in units:
            raise PermissionError(
                f"cannot file into '{requested}': not one of the viewer's owning units "
                f"({', '.join(sorted(units)) or 'none'}) (§3.3)"
            )
        return requested
    if not units:
        raise PermissionError(
            "cannot file: the viewer belongs to no owning unit, so the record would have "
            "to default to org-wide visibility (§3.3)"
        )
    if len(units) > 1:
        raise PermissionError(
            "the viewer belongs to several units — name the owning unit explicitly "
            f"(one of: {', '.join(sorted(units))}) (§3.3)"
        )
    return next(iter(units))


def _tier_ok(tier: ScopeTier, owner_unit: str | None, viewer: Viewer) -> bool:
    # org = everyone; personal/team/dept = viewer belongs to the record's owner_unit.
    # (A personal-tier record's owner_unit is a unit only its owner is in.)
    #
    # An org-tier row's `owner_unit` is DELIBERATELY not consulted: the tier already decided
    # the answer. What sits there is a maintainer record ("who curates this"), never a
    # permission. An org-tier term owned by, say, a curriculum unit is readable by every member
    # — intended (SPEC §3.3). The trap is reading the owner
    # column as a fence: it holds nothing shut. To narrow a row, change its TIER.
    if tier == ScopeTier.ORG:
        return True
    if owner_unit is None:
        return False
    return owner_unit in viewer.units


def _audience_ok(audience: ScopeAudience, viewer: Viewer) -> bool:
    if audience == ScopeAudience.PUBLIC:
        return True
    if audience == ScopeAudience.INTERNAL:
        return viewer.is_org_member
    if audience == ScopeAudience.PARTNER:
        return viewer.is_org_member or viewer.is_partner
    return False


def _sensitivity_ok(sensitivity: ScopeSensitivity, owner_unit: str | None, viewer: Viewer) -> bool:
    if sensitivity == ScopeSensitivity.NORMAL:
        return True
    # sensitive: owner_unit match OR an explicit grant for that unit.
    if owner_unit is None:
        return False
    return owner_unit in viewer.units or owner_unit in viewer.allowed_sensitive_units


def visible_to(scope: ScopeTags, owner_unit: str | None, viewer: Viewer) -> bool:
    """True iff ``viewer`` may see a record with these scope tags (§3.3). AND of all axes."""
    return (
        _tier_ok(scope.tier, owner_unit, viewer)
        and _audience_ok(scope.audience, viewer)
        and _sensitivity_ok(scope.sensitivity, owner_unit, viewer)
    )


def acl_filter(hits: list[TermHit], viewer: Viewer) -> list[TermHit]:
    """Drop any hit the viewer is not permitted to see (§3.2 step 4 — ★必須)."""
    return [h for h in hits if visible_to(h.scope, h.owner_unit, viewer)]


def acl_where_clause(viewer: Viewer, *, alias: str = "t") -> tuple[str, dict]:
    """Build a parameterized SQL WHERE fragment mirroring :func:`visible_to` (§3.3).

    Returns ``(sql_fragment, params)`` where ``sql_fragment`` is a boolean expression
    (no leading WHERE/AND) and ``params`` maps ``@name`` query parameters. The same
    rules run here AND in :func:`visible_to`, so a logic gap can't silently leak rows.

    Caller is responsible for binding ``params`` as BigQuery query parameters and for
    namespacing parameter names if combining multiple clauses.
    """
    a = alias
    units = sorted(viewer.units)
    sens_units = sorted(viewer.units | viewer.allowed_sensitive_units)

    params: dict = {
        "acl_is_org_member": viewer.is_org_member,
        "acl_is_partner": viewer.is_partner,
        "acl_units": units,
        "acl_sens_units": sens_units,
    }

    tier = (
        f"({a}.scope_tier = 'org' "
        f"OR ({a}.owner_unit IS NOT NULL AND {a}.owner_unit IN UNNEST(@acl_units)))"
    )
    audience = (
        f"({a}.scope_audience = 'public' "
        f"OR ({a}.scope_audience = 'internal' AND @acl_is_org_member) "
        f"OR ({a}.scope_audience = 'partner' AND (@acl_is_org_member OR @acl_is_partner)))"
    )
    sensitivity = (
        f"({a}.scope_sensitivity = 'normal' "
        f"OR ({a}.owner_unit IS NOT NULL AND {a}.owner_unit IN UNNEST(@acl_sens_units)))"
    )
    return f"({tier} AND {audience} AND {sensitivity})", params


# --- persons (§2.2 ②) — the roster is PII, so it gets the same double gate (§3.3) ---
#
# `persons` carries no scope_tier/audience/sensitivity columns, so `acl_where_clause` above
# cannot be reused as-is. The rule for people:
#
#   an org member may resolve ANY consenting active colleague, org-wide.
#
# That is the point of the layer — 誰のことを言っているのか分からないまま AI が答えるのを
# 防ぐ (§4.2) — and `consent_internal` is documented as 社内エキスパート探索・既定true
# (sql/ddl/02_persons.sql). Restricting resolution to the viewer's own unit would answer the
# wrong question: the risk is that `lookup_terms("職員", 100000)` ENUMERATES the roster, which
# is a question of HOW MANY rows may leave in one call, not of who may be named. That is bounded by
# `_K_CAPS["persons"]` (FLOOR-02), not here.
#
# What this gate does enforce: non-members get nothing, frozen (退職者) rows are never
# served, and an opt-out is honored for everyone except the person themselves.


def person_visible_to(person: Person, viewer: Viewer) -> bool:
    """True iff ``viewer`` may see this person (Gate 2, in-process twin of the SQL below).

    Fails closed on the axes it owns: a missing/unknown ``status`` is NOT treated as active,
    and a viewer with no ``person_id`` matches no one via the self branch (mirroring the SQL,
    which only self-matches on a non-empty id).
    """
    if person.status != "active":
        return False
    if not viewer.is_org_member:
        return False
    if viewer.person_id and person.person_id == viewer.person_id:
        return True   # 自分自身はオプトアウトしていても見える
    return bool(person.consent_internal)   # 社内公開はオプトアウト可 (§2.2 ②)


def person_disclosable_as_expert(person: Person, viewer: Viewer) -> bool:
    """True iff ``person`` may be **named as an expert candidate** to ``viewer`` (ERN §4c).

    `person_visible_to` の**部分集合**であって、別物ではない。違いは1点、**自分自身の例外を
    認めない**こと:

        viewer.is_org_member ∧ person.consent_internal ∧ person.status == 'active'

    名簿の解決（`person_visible_to`）では「本人はオプトアウトしていても自分を見られる」で
    よい — 自分の登録内容を確認できないほうが不便で、誰にも新しい事実は開かない。
    ところが ERN は違う。オプトアウトは「**エキスパート探索に出さないでくれ**」という
    表明そのもの（`sql/ddl/02_persons.sql` の consent_internal は「社内エキスパート探索」）
    なので、その人が自分で `find_expert` を呼んだときに自分が候補として返るのは、
    降ろしたはずの機能が本人にだけ動き続けている状態になる。

    SPEC_phase3 §4c は3条件を「**すべて**満たす人物のみ」と書いており、自分自身の例外に
    一言も触れていない。E4 も「consent_internal=false の人物は候補に出ない」と書いている。

    ERN 側に判定を写経させないために、規則はここ（`retrieval/acl.py`）に置く（E11）。
    """
    return (
        viewer.is_org_member
        and person.status == "active"
        and bool(person.consent_internal)
    )


def person_where_clause(viewer: Viewer, *, alias: str = "p") -> tuple[str, dict]:
    """SQL twin of :func:`person_visible_to` (Gate 1 — forbidden rows never leave BQ, §3.3).

    Same redundancy contract as :func:`acl_where_clause`: if you change one rule, change the
    other. A non-member gets ``FALSE`` — no roster, not even their own row.
    """
    a = alias
    params: dict = {
        "acl_p_is_org_member": viewer.is_org_member,
        "acl_p_self": viewer.person_id or "",
    }
    # The self branch requires a non-empty id, or a viewer with person_id=None would match
    # any row whose person_id is the empty string (the in-process twin refuses that too).
    body = (
        f"((@acl_p_self != '' AND {a}.person_id = @acl_p_self) "
        f"OR IFNULL({a}.consent_internal, FALSE))"
    )
    return f"({a}.status = 'active' AND @acl_p_is_org_member AND {body})", params
