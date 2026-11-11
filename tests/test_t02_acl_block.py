"""T2 — ACL 遮断: sensitive/dept 用語を権限なし閲覧者に返さない（WHERE句でも除外）(§8)."""

from __future__ import annotations

from waca_loop.models import (
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
)
from waca_loop.retrieval.acl import acl_where_clause, visible_to

_SENSITIVE_DEPT = ScopeTags(ScopeTier.DEPT, ScopeAudience.INTERNAL, ScopeSensitivity.SENSITIVE)


def test_sensitive_dept_hidden_from_other_unit(term_store, embedder, viewer_member):
    from waca_loop.retrieval.hybrid import hybrid_search

    hits = hybrid_search(
        "Snowprism", viewer_member, 5, store=term_store, embedder=embedder
    )
    assert all(h.term != "Snowprism" for h in hits), "dept_sales sensitive term must be blocked"


def test_sensitive_dept_visible_to_owning_unit(term_store, embedder, viewer_sales):
    from waca_loop.retrieval.hybrid import hybrid_search

    hits = hybrid_search("Snowprism", viewer_sales, 5, store=term_store, embedder=embedder)
    assert any(h.term == "Snowprism" for h in hits), "owning unit should see its sensitive term"


def test_visible_to_rules(viewer_member, viewer_sales, viewer_outsider):
    assert not visible_to(_SENSITIVE_DEPT, "dept_sales", viewer_member)
    assert visible_to(_SENSITIVE_DEPT, "dept_sales", viewer_sales)
    assert not visible_to(_SENSITIVE_DEPT, "dept_sales", viewer_outsider)


def test_where_clause_mirrors_rules(viewer_member):
    sql, params = acl_where_clause(viewer_member, alias="t")
    # The SQL gate must reference all three axes (§3.3 二重で効かせる).
    assert "scope_tier" in sql
    assert "scope_audience" in sql
    assert "scope_sensitivity" in sql
    assert params["acl_units"] == ["dept_eng"]
    assert params["acl_is_org_member"] is True
