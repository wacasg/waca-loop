"""SPEC_ops P0 — server-side guards: session scope (R1), org gate (R2), coverage trail
(R3a), use_category normalization (R8), and policy_row_ids as a REPEATED column.

All offline (InMemory stores + guard pure functions), per the repo invariants.
"""

from __future__ import annotations

import inspect

import pytest

from waca_loop.guard import (
    SessionScopeRegistry,
    is_org_related,
    looks_non_org,
    normalize_data_kinds,
    normalize_use_category,
)
from waca_loop.ledger.tool_events import InMemoryToolEventStore, coverage_mismatch
from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.models import Viewer
from waca_loop.server import ServerContext


def _ctx(term_store, embedder, viewer, **kw) -> ServerContext:
    return ServerContext(
        term_store=term_store,
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=viewer,
        **kw,
    )


@pytest.fixture
def member() -> Viewer:
    return Viewer(subject="sub-m", person_id="p_m", is_org_member=True,
                  units=frozenset({"dept_eng"}))


@pytest.fixture
def steward() -> Viewer:
    return Viewer(subject="sub-s", person_id="p_s", is_org_member=True,
                  units=frozenset({"steward", "tech"}))


# --- R8: use_category normalization ---------------------------------------------------------

def test_normalize_accepts_forgiving_variants():
    assert normalize_use_category("l2") == "l2"
    assert normalize_use_category(" L2 ") == "l2"
    assert normalize_use_category("prohibited") == "prohibited"
    assert normalize_use_category("禁止") == "prohibited"


def test_normalize_maps_three_colour_tokens():
    """三色 tokens on input: clients using them keep recording (🟢→l2 / 🟡→l1 / 🔴→l0)."""
    assert normalize_use_category("green") == "l2"
    assert normalize_use_category("🟢") == "l2"
    assert normalize_use_category("🟡") == "l1"
    assert normalize_use_category("赤") == "l0"


def test_normalize_rejects_unknown_naming_valid_values():
    with pytest.raises(ValueError, match="l0|l1|l2|prohibited"):
        normalize_use_category("blue")


def test_log_usage_accepts_emoji_category(term_store, embedder, member):
    ctx = _ctx(term_store, embedder, member)
    out = ctx.log_usage("🟢", ["none"], "サンプル協会 台帳の動作確認", "test")
    assert "log_id" in out
    assert ctx.usage_store.rows[0]["use_category"] == "l2"


# --- REPEATED policy_row_ids must never be None ----------------------------------------------

def test_omitted_policy_row_ids_becomes_empty_list(term_store, embedder, member):
    ctx = _ctx(term_store, embedder, member)
    ctx.log_usage("l2", ["none"], "サンプル協会 記録テスト", "test")
    assert ctx.usage_store.rows[0]["policy_row_ids"] == []  # not None (BQ REPEATED)


# --- R1: session scope ------------------------------------------------------------------------

def test_external_scope_blocks_all_writes(term_store, embedder, member):
    registry = SessionScopeRegistry()
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)

    out = ctx.set_session_scope("external", "他組織の業務")
    assert out["scope"] == "external"

    refused = ctx.log_usage("l2", ["none"], "他社の議事メモ", "test")
    assert refused["refused"] is True and refused["recorded"] is False
    assert "AI生成表示" not in refused["reason"]   # 拒否文に AI 生成表示の文言は入れない
    assert ctx.usage_store.rows == []                       # nothing written
    # 拒否は**自己診断可能**であること。宣言は利用者に紐づいて TTL まで残るので、
    # 拒否文だけを読んだ人が「なぜ」と「どう戻すか」に到達できなければ、数セッションに
    # わたり原因不明のまま記録が落ち続ける。
    assert refused["scope"] == "external"                   # 機械可読な場として出す
    assert "external" in refused["reason"]                  # 立っている宣言そのもの
    assert "set_session_scope" in refused["reason"]         # 戻し方
    assert "またいで" in refused["reason"]                    # セッションを越えて残ること

    # Other write tools are refused the same way (before store checks fire).
    assert ctx.propose_candidates([{"kind": "term"}])["refused"] is True
    assert ctx.add_sfaq_question("問い")["refused"] is True
    assert ctx.add_sfaq_feedback("q_x", "agree", "b")["refused"] is True
    assert ctx.submit_survey_response(5, "r", "u", "h", False)["refused"] is True


def test_org_scope_records_and_registry_is_per_subject(term_store, embedder, member):
    registry = SessionScopeRegistry()
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    ctx.set_session_scope("org")
    ctx.log_usage("l2", ["none"], "サンプル協会 議事メモ整形", "test")
    assert len(ctx.usage_store.rows) == 1

    # A different subject's declaration doesn't leak onto this one.
    registry.declare(actor_id="someone-else", actor_raw=None, scope="external")
    ctx.log_usage("l2", ["none"], "サンプル協会 議事メモ整形2", "test")
    assert len(ctx.usage_store.rows) == 2


def test_excluded_declarations_counted_without_content():
    registry = SessionScopeRegistry()
    registry.declare(actor_id="a", actor_raw=None, scope="external")
    registry.declare(actor_id="b", actor_raw=None, scope="private")
    registry.declare(actor_id="c", actor_raw=None, scope="org")
    assert registry.excluded_declarations == 2              # counts only — no reasons stored


def test_undeclared_scope_keeps_recording(term_store, embedder, member):
    """R1 acceptance: sessions that never declare a scope are recorded normally."""
    ctx = _ctx(term_store, embedder, member, scope_registry=SessionScopeRegistry())
    ctx.log_usage("l2", ["none"], "サンプル協会 台帳テスト", "test")
    assert len(ctx.usage_store.rows) == 1


# --- R2: org-context gate --------------------------------------------------------------------

def test_unrelated_org_context_goes_pending(term_store, embedder, member):
    """No サンプル協会 linkage but no other-affiliation signal either → pending, steward reviews."""
    ctx = _ctx(term_store, embedder, member)
    out = ctx.log_usage(
        "l2", ["none"], "見積書の体裁を整える", "test", org_context="○○社の案件"
    )
    assert out["status"] == "needs_confirmation"
    assert ctx.usage_store.rows[0]["status"] == "pending"


def test_org_context_records(term_store, embedder, member):
    ctx = _ctx(term_store, embedder, member)
    ctx.log_usage("l2", ["none"], "議事メモ整形", "test", org_context="サンプル協会事務局の定例")
    assert ctx.usage_store.rows[0]["status"] == "recorded"


def test_policy_anchor_counts_as_org_linkage(term_store, embedder, member):
    ctx = _ctx(term_store, embedder, member)
    ctx.log_usage("l2", ["none"], "無関係に見える要約", "test",
                  policy_row_ids=["ORG-3"], org_context="…")
    assert ctx.usage_store.rows[0]["status"] == "recorded"


def test_absent_org_context_records_unless_strict(term_store, embedder, member):
    ctx = _ctx(term_store, embedder, member)                       # strict off (default)
    ctx.log_usage("l2", ["none"], "何かの要約", "test")
    assert ctx.usage_store.rows[0]["status"] == "recorded"          # default: records

    strict = _ctx(term_store, embedder, member, org_guard_strict=True)
    out = strict.log_usage("l2", ["none"], "何かの要約", "test")
    assert out["status"] == "needs_confirmation"
    assert strict.usage_store.rows[0]["status"] == "pending"


def test_is_org_related_matches_keywords():
    assert is_org_related("定例研修の教材改訂")
    assert is_org_related(None, "サンプル協会会員データ")
    assert not is_org_related("○○大学の講義", None)


# --- R2: steward review ----------------------------------------------------------------------

def test_steward_approves_and_discards_pending(term_store, embedder, member, steward):
    store = InMemoryUsageLogStore()
    ctx = ServerContext(term_store=term_store, usage_store=store, embedder=embedder,
                        viewer=member)
    ctx.log_usage("l2", ["none"], "帰属不明の要約", "test", org_context="不明な文脈")
    ctx.log_usage("l2", ["none"], "別の帰属不明", "test", org_context="別の不明")
    pending_ids = [r["log_id"] for r in store.rows if r["status"] == "pending"]
    assert len(pending_ids) == 2

    sctx = ServerContext(term_store=term_store, usage_store=store, embedder=embedder,
                         viewer=steward)
    sctx.review_pending_usage(pending_ids[0], "approve")
    assert store.rows[0]["status"] == "recorded"
    sctx.review_pending_usage(pending_ids[1], "discard")
    assert all(r["log_id"] != pending_ids[1] for r in store.rows)   # physically gone


def test_non_steward_cannot_review(term_store, embedder, member):
    ctx = _ctx(term_store, embedder, member)
    with pytest.raises(PermissionError):
        ctx.review_pending_usage("x", "approve")


# --- R3a: tool-event trail + coverage --------------------------------------------------------

def test_policy_check_and_usage_leave_trail(term_store, embedder, member):
    events = InMemoryToolEventStore()
    ctx = _ctx(term_store, embedder, member, tool_event_store=events)
    ctx.get_applicable_policy()
    ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")
    tools = [r["tool"] for r in events.rows]
    assert tools == ["policy_check", "usage_recorded"]
    assert events.rows[0]["actor_id"] == "p_m"              # ledger-style attribution
    assert all("summary" not in r for r in events.rows)      # thin rows, no content


def test_trail_failure_never_breaks_the_tool(term_store, embedder, member):
    class Exploding:
        def record(self, *a, **k):
            raise RuntimeError("trail down")

    ctx = _ctx(term_store, embedder, member, tool_event_store=Exploding())
    out = ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")   # must not raise
    assert "log_id" in out


def test_coverage_mismatch_counts_checks_without_writes():
    rows = [
        {"occurred_at": "2026-07-20T01:00:00+00:00", "tool": "policy_check", "actor_id": "p"},
        {"occurred_at": "2026-07-20T02:00:00+00:00", "tool": "policy_check", "actor_id": "p"},
        {"occurred_at": "2026-07-20T03:00:00+00:00", "tool": "usage_recorded", "actor_id": "p"},
        {"occurred_at": "2026-07-21T01:00:00+00:00", "tool": "usage_recorded", "actor_id": "p"},
    ]
    report = coverage_mismatch(rows)
    assert report["2026-07-20/p"]["mismatch"] == 1          # 2 checks, 1 write
    assert report["2026-07-21/p"]["mismatch"] == 0          # writes may exceed checks


# --- R1b: ask before writing a summary that looks like another affiliation's ------------------

def test_non_org_signal_asks_instead_of_writing(term_store, embedder, member):
    """An undeclared session + a 大学/私事 signal must not put the summary in サンプル協会's ledger.

    Filing it as ``pending`` would already write the summary — the exact thing operating
    rule 2 (SPEC_ops §6) forbids for 他組織/私事 work.
    """
    ctx = _ctx(term_store, embedder, member, scope_registry=SessionScopeRegistry())
    out = ctx.log_usage(
        "l2", ["none"], "講義スライドの整形", "test", org_context="○○大学の授業準備"
    )
    assert out["needs_scope_confirmation"] is True
    assert out["recorded"] is False
    assert ctx.usage_store.rows == []                       # nothing reached the ledger


def test_standing_org_does_not_silence_the_per_turn_content_gate(
    term_store, embedder, member
):
    """`org` は external/private と**同じだけ** sticky（利用者単位・保存され、宣言し直すか
    TTL の失効まで続く）。R1b を「宣言があれば免除」にすると、一度 org を宣言した人の
    R1b が以後**永久に**無効になり、私事の要約が組織の台帳に入り得る。
    拒否文が「org を宣言して解除してください」と案内する以上、その経路は塞いでおく必要がある。
    """
    ctx = _ctx(term_store, embedder, member, scope_registry=SessionScopeRegistry())
    ctx.set_session_scope("org")
    out = ctx.log_usage("l2", ["none"], "確定申告の書類整理", "test",
                        org_context="○○大学の授業準備")
    assert out["needs_scope_confirmation"] is True
    assert ctx.usage_store.rows == []            # 台帳に入っていない


def test_the_content_gate_has_a_way_out_that_terminates(term_store, embedder, member):
    """免除が無い以上、抜け道は**実際に抜けられる**こと — さもないと無限ループになる。

    案内文が指すのは「その1件がサンプル協会業務である証拠」の側 (org_context / policy_row_ids)。
    """
    from waca_loop.guard import SCOPE_CONFIRMATION_MESSAGE

    ctx = _ctx(term_store, embedder, member, scope_registry=SessionScopeRegistry())
    blocked = ctx.log_usage("l2", ["none"], "講義スライドの整形", "test",
                            org_context="○○大学の授業準備")
    assert blocked["needs_scope_confirmation"] is True
    # 案内文が set_session_scope を解除手段として案内していないこと（もう効かないので）
    assert "set_session_scope では解除できません" in SCOPE_CONFIRMATION_MESSAGE

    # 出口1: org_context に サンプル協会 との関連を書く
    ctx.log_usage("l2", ["none"], "講義スライドの整形", "test",
                  org_context="サンプル協会 の大学向け団体研修の授業準備")
    assert len(ctx.usage_store.rows) == 1
    # 出口2: policy 行に紐づける
    ctx.log_usage("l2", ["none"], "講義スライドの整形", "test",
                  org_context="○○大学の授業準備", policy_row_ids=["ORG-3"])
    assert len(ctx.usage_store.rows) == 2


def test_policy_anchor_skips_the_scope_prompt(term_store, embedder, member):
    """Anchored to a policy row = already サンプル協会-linked; never ask (§9.3)."""
    ctx = _ctx(term_store, embedder, member, scope_registry=SessionScopeRegistry())
    ctx.log_usage("l2", ["none"], "大学向け団体研修の案内文", "test",
                  policy_row_ids=["ORG-3"])
    assert len(ctx.usage_store.rows) == 1


def test_looks_non_org_lets_org_win_ties():
    assert looks_non_org("○○大学の講義スライド")
    assert not looks_non_org("大学向けのサンプル協会団体研修")   # サンプル協会 signal wins
    assert not looks_non_org("議事メモの整形")           # no signal either way


# --- R9: data_kinds vocabulary normalization --------------------------------------------------

def test_data_kinds_map_onto_the_canonical_vocabulary():
    kinds, unmapped = normalize_data_kinds(["internal_docs", "source_code", "pii"])
    assert kinds == ["internal", "pii"]                     # de-duplicated, order preserved
    assert unmapped == []


def test_free_text_data_kind_is_reported_but_never_dropped():
    kinds, unmapped = normalize_data_kinds(["組織体制・役職情報（連絡先等の個人情報なし）"])
    assert kinds == ["internal"]                            # 個人情報なし → internal, not pii
    assert unmapped == ["組織体制・役職情報（連絡先等の個人情報なし）"]


def test_free_text_mentioning_personal_data_lands_on_pii():
    """Never fold an unrecognised sensitive value into a bland ``internal`` (§9.4)."""
    kinds, _ = normalize_data_kinds(["受講者名簿(氏名・メールアドレス入り)"])
    assert kinds == ["pii"]


def test_empty_data_kinds_become_none():
    assert normalize_data_kinds([]) == (["none"], [])
    assert normalize_data_kinds("pii") == (["pii"], [])      # bare string ≠ N characters


def test_policy_trigger_kinds_pass_through_unchanged():
    """A rule's trigger_kinds are the other half of this axis — folding them mutes the rule."""
    kinds, unmapped = normalize_data_kinds(["raw_export"], allowed_extra={"raw_export"})
    assert kinds == ["raw_export"] and unmapped == []


def test_log_usage_reports_unmapped_kinds_but_records_anyway(term_store, embedder, member):
    ctx = _ctx(term_store, embedder, member)
    out = ctx.log_usage("l2", ["社内メモ・雑多"], "サンプル協会事務局の議事メモ整形", "test")
    assert out["unmapped_data_kinds"] == ["社内メモ・雑多"]
    assert out["data_kinds_recorded"] == ["internal"]
    assert ctx.usage_store.rows[0]["data_kinds"] == ["internal"]


# --- R3a 例外: discard の証跡 ------------------------------------------------


def test_discard_leaves_an_irreversible_trail(term_store, embedder, member, steward):
    """Discard physically deletes the row, so the ACT must be trailed.

    Without this event nothing in the system records that the pending row ever existed.
    """
    events = InMemoryToolEventStore()
    store = InMemoryUsageLogStore()
    ctx = ServerContext(term_store=term_store, usage_store=store, embedder=embedder,
                        viewer=member, tool_event_store=events)
    ctx.log_usage("l2", ["none"], "帰属不明の要約", "test", org_context="不明な文脈")
    log_id = store.rows[0]["log_id"]

    sctx = ServerContext(term_store=term_store, usage_store=store, embedder=embedder,
                         viewer=steward, tool_event_store=events)
    sctx.review_pending_usage(log_id, "discard")

    assert store.rows == []                                   # row physically gone
    reviewed = [r for r in events.rows if r["tool"] == "usage_reviewed"]
    assert len(reviewed) == 1
    assert reviewed[0]["detail"] == f"discard:{log_id}"        # who/when/what, no content
    assert reviewed[0]["actor_id"] == steward.person_id
    assert "summary" not in reviewed[0] and "帰属不明" not in str(reviewed[0])


def test_discard_is_fail_closed_when_the_trail_cannot_be_written(
    term_store, embedder, member, steward
):
    """R3a 例外 — a trail store that fails must abort the delete, not lose the record."""
    class Exploding:
        def record(self, *a, **k):
            raise RuntimeError("trail down")

    store = InMemoryUsageLogStore()
    ctx = ServerContext(term_store=term_store, usage_store=store, embedder=embedder,
                        viewer=member)
    ctx.log_usage("l2", ["none"], "帰属不明の要約", "test", org_context="不明な文脈")
    log_id = store.rows[0]["log_id"]

    sctx = ServerContext(term_store=term_store, usage_store=store, embedder=embedder,
                         viewer=steward, tool_event_store=Exploding())
    with pytest.raises(RuntimeError):
        sctx.review_pending_usage(log_id, "discard")
    assert len(store.rows) == 1, "no audit record -> no deletion"

    # approve stays best-effort: observability must not block a non-destructive act.
    assert sctx.review_pending_usage(log_id, "approve")["done"] is True


def test_reviewed_detail_stays_a_closed_shape(term_store, embedder, steward):
    """tool_events must not become a free-text channel (DDL: 内容は一切持たない).

    A length cap is not a shape: 64 characters of prose passed as a ``log_id`` would land
    verbatim in the trail and be printed to an operator console by ``coverage_report``.
    Ledger ids are UUIDs, so a non-id is refused **before** anything is written — while an
    unknown-but-well-formed id still leaves its attempt on the record.
    """
    import uuid

    events = InMemoryToolEventStore()
    store = InMemoryUsageLogStore()
    sctx = ServerContext(term_store=term_store, usage_store=store, embedder=embedder,
                         viewer=steward, tool_event_store=events)

    with pytest.raises(ValueError):
        sctx.review_pending_usage("田中さんの給与相談の記録を消して", "discard")
    assert events.rows == [], "自由文は証跡に1文字も書かれてはいけない"

    unknown = str(uuid.uuid4())
    with pytest.raises(ValueError):        # 実在しない id — 証跡は削除の *前* に書かれる
        sctx.review_pending_usage(unknown, "discard")
    assert events.rows[0]["detail"] == f"discard:{unknown}"


def test_refused_review_leaves_no_trail(term_store, embedder, member):
    """A non-steward attempt is rejected before the trail — no forged audit rows."""
    events = InMemoryToolEventStore()
    ctx = _ctx(term_store, embedder, member, tool_event_store=events)
    with pytest.raises(PermissionError):
        ctx.review_pending_usage("x", "discard")
    assert [r for r in events.rows if r["tool"] == "usage_reviewed"] == []


def test_coverage_sql_selects_detail():
    """The discard listing reads `detail` — if the SELECT drops it, the report silently
    shows 0 discards forever."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from coverage_report import build_sql

    assert "detail" in build_sql("p.d.tool_events", 7)


# --- C4: サーバー側 nudge (SPEC_coverage §3 C4) ------------------------------------------------
#
# AC-3: 自分の未記録ヒントが出る／他 viewer の情報は出ない。フックの無い経路（ブラウザ版・
# 他の AI クライアント）にも効かせるための、クライアント非依存の督促。


def _turn(ctx, *, record: bool) -> None:
    """One turn as the 運用ルール describes it: policy first, ledger write after (maybe)."""
    ctx.get_applicable_policy()
    if record:
        ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")


def test_hint_reports_the_callers_own_unrecorded_turns(
    term_store, embedder, member, policy_store
):
    events = InMemoryToolEventStore()
    ctx = _ctx(term_store, embedder, member, tool_event_store=events,
               policy_store=policy_store)
    _turn(ctx, record=True)
    _turn(ctx, record=False)
    _turn(ctx, record=False)
    out = ctx.get_applicable_policy()          # the 4th check is in flight — not counted
    assert out["coverage_hint"] == (
        "直近3回のポリシー確認のうち2回、記録が残っていません"
        "（log_usage の呼び忘れかもしれません。助言であり、利用は止めません）"
    )


def test_no_hint_when_every_turn_was_recorded(term_store, embedder, member, policy_store):
    """Advice that is always on is noise — silence is the default (§3 C4)."""
    events = InMemoryToolEventStore()
    ctx = _ctx(term_store, embedder, member, tool_event_store=events,
               policy_store=policy_store)
    for _ in range(4):
        _turn(ctx, record=True)
    assert "coverage_hint" not in ctx.get_applicable_policy()


def test_first_call_of_a_session_is_never_nagged(term_store, embedder, member, policy_store):
    """get_applicable_policy runs BEFORE log_usage by design, so the current turn is
    structurally open. Counting it would fire the nudge on every session's first call."""
    events = InMemoryToolEventStore()
    ctx = _ctx(term_store, embedder, member, tool_event_store=events,
               policy_store=policy_store)
    assert "coverage_hint" not in ctx.get_applicable_policy()
    assert "coverage_hint" not in ctx.get_applicable_policy()   # one completed check: too thin


def test_hint_never_carries_another_viewers_coverage(
    term_store, embedder, member, steward, policy_store
):
    """★ 制約1: 自分の actor_id 分のみ。他人の未記録は絶対に出さない。

    A shared trail store holding a colleague's dismal record must not move this viewer's
    hint by one turn — the fact of who is not logging their AI use never travels sideways.
    """
    events = InMemoryToolEventStore()
    other = _ctx(term_store, embedder, steward, tool_event_store=events,
                 policy_store=policy_store)
    for _ in range(6):
        _turn(other, record=False)             # the colleague records nothing at all

    mine = _ctx(term_store, embedder, member, tool_event_store=events,
                policy_store=policy_store)
    for _ in range(4):
        _turn(mine, record=True)               # I record every turn
    assert "coverage_hint" not in mine.get_applicable_policy()

    # ...and the store itself refuses to hand over the other actor's rows.
    rows = events.recent_by_actor(actor_id=member.person_id, actor_raw=None)
    assert rows and all(r["actor_id"] == member.person_id for r in rows)


def test_unresolved_viewers_are_not_pooled_together(term_store, embedder, policy_store):
    """§5: unresolved → hashed actor_raw. Two anonymous callers are two actors, not one."""
    events = InMemoryToolEventStore()
    a = _ctx(term_store, embedder, Viewer.anonymous("sub-a"), tool_event_store=events,
             policy_store=policy_store)
    for _ in range(6):
        _turn(a, record=False)
    b = _ctx(term_store, embedder, Viewer.anonymous("sub-b"), tool_event_store=events,
             policy_store=policy_store)
    assert "coverage_hint" not in b.get_applicable_policy()


def test_hint_failure_never_costs_the_viewer_their_policy(
    term_store, embedder, member, policy_store
):
    """★ 制約2: 助言であってブロックではない。ヒント計算が落ちても本体は必ず返る。"""
    class HalfBroken(InMemoryToolEventStore):
        def recent_by_actor(self, **kw):
            raise RuntimeError("trail query down")

    ctx = _ctx(term_store, embedder, member, tool_event_store=HalfBroken(),
               policy_store=policy_store)
    out = ctx.get_applicable_policy()
    assert "coverage_hint" not in out
    assert out["floor"] and out["legend"]        # the policy itself is served in full


def test_hint_works_without_a_trail_store_at_all(term_store, embedder, member, policy_store):
    """stdio/tests wire no trail store; the tool must not care."""
    ctx = _ctx(term_store, embedder, member, policy_store=policy_store)
    assert "coverage_hint" not in ctx.get_applicable_policy()


def test_coverage_hint_pairs_checks_with_the_write_that_follows():
    """Pure-function edges: a write with no preceding check pairs with nothing."""
    from waca_loop.ledger.tool_events import coverage_hint

    def rows(*tools):
        return [{"tool": t} for t in tools]

    # write before any check — must not retroactively close a later check
    assert coverage_hint(rows(
        "usage_recorded", "policy_check", "policy_check", "policy_check"
    )) == (
        "直近2回のポリシー確認のうち2回、記録が残っていません"
        "（log_usage の呼び忘れかもしれません。助言であり、利用は止めません）"
    )
    # two writes for one check (a turn that logged twice) still closes exactly that check
    assert coverage_hint(rows(
        "policy_check", "usage_recorded", "usage_recorded", "policy_check", "usage_recorded",
        "policy_check",
    )) is None


def test_window_bounds_how_far_back_the_nudge_looks():
    """Old misses age out — the nudge is about the last few turns, not a permanent record."""
    from waca_loop.ledger.tool_events import COVERAGE_HINT_WINDOW, coverage_hint

    old_misses = ["policy_check"] * 4
    recent_good = ["policy_check", "usage_recorded"] * COVERAGE_HINT_WINDOW
    rows = [{"tool": t} for t in [*old_misses, *recent_good, "policy_check"]]
    assert coverage_hint(rows) is None


def test_hint_second_gate_holds_when_the_store_gate_fails(term_store, embedder, member,
                                                          policy_store):
    """★ §3.3 の二重化。取得側の門番が壊れた日に、ヒントが他人の記録状況を数えないこと。

    本番で走るのは SQL のほうなので、そこが1枚だけにならないことの表明。取得側が
    **故意に他 actor の行を混ぜて返す**ストアを挿し、それでもヒントが出ないことを確かめる。
    片肺なら、ここで同僚の未記録が自分の数字になる。
    """
    class LeakyStore(InMemoryToolEventStore):
        def recent_by_actor(self, **kw):
            return [{k: r.get(k) for k in
                     ("event_id", "occurred_at", "tool", "actor_id", "actor_raw")}
                    for r in self.rows]           # 誰の行でも返してしまう

    events = LeakyStore()
    other = ServerContext(term_store=term_store, usage_store=InMemoryUsageLogStore(),
                          embedder=embedder, viewer=steward_of("p_other"),
                          policy_store=policy_store, tool_event_store=events)
    for _ in range(6):
        other.get_applicable_policy()             # 同僚は6回確認して1回も記録しない

    mine = _ctx(term_store, embedder, member, tool_event_store=events,
                policy_store=policy_store)
    assert "coverage_hint" not in mine.get_applicable_policy()


def steward_of(person_id: str) -> Viewer:
    return Viewer(subject=f"sub-{person_id}", person_id=person_id, is_org_member=True,
                  units=frozenset({"dept_eng"}))


# --- C4: 確認と記録の時系列のリプレイ ------------------------------------------
#
# 「直近5回のポリシー確認のうち3回、記録が残っていません」が出る、actor 1名分の
# `tool_events` の列。過大計上（1ターンで2回確認）と真の未記録を、**同じテストの陽性側と
# 陰性側**で測る。行は tool 名と時刻だけで、内容は持たない（tool_events は detail 以外を
# 持たない設計）。
#
# 判別は時刻の間隔で行う: 同一ターン内の連続確認は秒オーダー、別ターンは分〜時間オーダー。

def _trail_rows(*pairs) -> list[dict]:
    """``(時刻, tool)`` の並びを recent_by_actor と同じ形に。"""
    return [
        {"event_id": f"e{i}", "occurred_at": ts, "tool": tool,
         "actor_id": "p_replay", "actor_raw": None}
        for i, (ts, tool) in enumerate(pairs)
    ]


#: 合成した列。後半の3回の確認はそれぞれ 1.8h / 2.1h / 10.3h 離れており、
#: どれも「1ターンで2回確認」ではない — 別々のターンの**真の未記録**である。
_SYNTHETIC_TRAIL = (
    ("2026-01-15T14:05:20", "policy_check"),
    ("2026-01-15T14:07:22", "usage_recorded"),
    ("2026-01-15T14:12:27", "policy_check"),
    ("2026-01-15T14:17:53", "usage_recorded"),
    ("2026-01-16T07:30:31", "policy_check"),      # ← 未記録
    ("2026-01-16T09:19:15", "policy_check"),      # ← 未記録
    ("2026-01-16T11:25:07", "policy_check"),      # ← 未記録
    ("2026-01-16T21:42:03", "policy_check"),      # ← このターンで hint が出る
)


def test_replay_of_a_trail_reproduces_the_expected_hint():
    """★ 陽性側: 本当に記録が落ちているとき、この列から期待どおりの1行が出ること。

    最後の確認で `get_applicable_policy` が返す文面をそのまま再現する。ここが
    赤くなったら、実際に表示される助言とテストが別物になっている。
    """
    from waca_loop.ledger.tool_events import coverage_hint

    assert coverage_hint(_trail_rows(*_SYNTHETIC_TRAIL)) == (
        "直近5回のポリシー確認のうち3回、記録が残っていません"
        "（log_usage の呼び忘れかもしれません。助言であり、利用は止めません）"
    )


def test_the_same_turns_recorded_properly_say_nothing():
    """★ 陰性側: 同じターン数・同じ並びでも、各確認の後に記録があれば黙ること。

    陽性だけを緑にしても意味がない（督促を弱めれば陽性は消せる）。同じ列の「正しく
    記録した版」で黙ることまで測って、はじめて閾値をいじっていないと言える。
    """
    from waca_loop.ledger.tool_events import coverage_hint

    recorded = []
    for ts, tool in _SYNTHETIC_TRAIL:
        recorded.append((ts, tool))
        if tool == "policy_check":
            recorded.append((ts, "usage_recorded"))   # そのターンで記録した
    assert coverage_hint(_trail_rows(*recorded)) is None


def test_two_checks_in_one_turn_are_still_counted_as_one_miss():
    """★ docstring が認めている既知の過大計上を、時系列で固定する（C5 までの代償）。

    16:28:50 / 16:28:55 / 16:29:33 — 同一ターンで3回確認し、記録は1回。
    間隔は 5 秒と 38 秒で、別ターンの間隔（分〜時間）とは桁が違う。2件が過大計上として
    数えられることを**そのまま**書き留める。ここを黙らせる直し方（連続確認をまとめる）は
    未記録が続く N ターンも1つにまとめてしまうので採らない（SPEC_coverage §3 C4）。
    """
    from waca_loop.ledger.tool_events import coverage_hint

    rows = _trail_rows(
        ("2026-09-12T12:00:00", "policy_check"),
        ("2026-09-12T12:00:10", "usage_recorded"),
        ("2026-09-12T16:28:50", "policy_check"),      # 同一ターン 1回目
        ("2026-09-12T16:28:55", "policy_check"),      # 同一ターン 2回目（+5s）
        ("2026-09-12T16:29:33", "policy_check"),      # 同一ターン 3回目（+38s）
        ("2026-09-12T16:55:20", "usage_recorded"),    # 記録は1回
        ("2026-09-12T18:19:05", "policy_check"),
    )
    assert coverage_hint(rows) == (
        "直近4回のポリシー確認のうち2回、記録が残っていません"
        "（log_usage の呼び忘れかもしれません。助言であり、利用は止めません）"
    )


def test_one_persons_many_clients_share_one_coverage_stream(term_store, embedder, member,
                                                            policy_store):
    """★ 軌跡は actor 単位で、クライアント単位ではない。

    同じ人が複数の AI クライアントや自動実行を並行して動かすと、確認と記録は**1本の列**に
    入る。記録しない自動実行が混ざると、きちんと記録している対話セッションに督促が出る。
    仕様どおりの挙動だが、督促を受け取った側が「自分は毎ターン記録している」と感じる
    ずれの正体はここなので、テストで明示しておく。
    """
    bot = _ctx(term_store, embedder, member, policy_store=policy_store,
               tool_event_store=(events := InMemoryToolEventStore()))
    me = _ctx(term_store, embedder, member, policy_store=policy_store,
              tool_event_store=events)

    for _ in range(3):
        bot.get_applicable_policy()               # 自動実行: 確認だけして記録しない
    _turn(me, record=True)                        # 私はきちんと記録している
    _turn(me, record=True)

    hint = me.get_applicable_policy().get("coverage_hint")
    assert hint is not None and "3回、記録が残っていません" in hint


def test_a_session_declared_out_of_scope_is_never_nagged(term_store, embedder, member,
                                                         policy_store):
    """★ R1 と C4 の衝突。external/private を正しく宣言した人を督促してはいけない。

    宣言済みセッションでは `log_usage` が R1 で拒否され `usage_recorded` が出ない一方、
    `policy_check` は出続ける。放っておくとヒントは「記録が残っていません」を出し続け、
    それを止める一番安いやり方は **他組織・私事の要約を サンプル協会 の台帳に書くこと** になる。
    運用ルール2（SPEC_ops §6）と R1 が防いでいるものを、督促が押し破る形。
    """
    from waca_loop.guard import SessionScopeRegistry

    registry = SessionScopeRegistry()
    events = InMemoryToolEventStore()
    ctx = ServerContext(term_store=term_store, usage_store=InMemoryUsageLogStore(),
                        embedder=embedder, viewer=member, policy_store=policy_store,
                        tool_event_store=events, scope_registry=registry)
    ctx.set_session_scope("external", "大学の業務")
    for _ in range(5):
        ctx.get_applicable_policy()
        ctx.log_usage("l2", ["none"], "大学の講義準備", "test")   # R1 が拒否 → 台帳行は出ない

    assert [r["tool"] for r in events.rows].count("usage_recorded") == 0   # 前提の確認
    assert "coverage_hint" not in ctx.get_applicable_policy()

    # org に戻したら、そこから先は普通に効く。
    registry.declare(actor_id=member.person_id, actor_raw=None, scope="org")
    for _ in range(3):
        ctx.get_applicable_policy()
    assert "記録が残っていません" in ctx.get_applicable_policy()["coverage_hint"]


def test_a_blank_identity_never_forms_a_bucket():
    """空文字の identity 同士が同じ actor として束ねられないこと。"""
    from waca_loop.ledger.tool_events import same_actor

    assert same_actor({"actor_id": "", "actor_raw": ""}, None, "") is False
    assert same_actor({"actor_id": "", "actor_raw": ""}, "", "") is False
    assert same_actor({"actor_id": None, "actor_raw": "h"}, None, "h") is True


def test_recent_by_actor_sql_is_bounded_and_deterministic():
    """SQL の形の表明（`test_coverage_sql_selects_detail` と同じ流儀）。

    行で確かめられない2点をここで押さえる: 同時刻の並び順（行では実装依存で揺れて
    偽陰性になる）と、パーティション述語（無いと毎ターン全社の証跡を全走査する）。
    """
    import inspect

    from waca_loop.ledger.tool_events import BigQueryToolEventStore

    sql = inspect.getsource(BigQueryToolEventStore.recent_by_actor)
    assert "ORDER BY occurred_at DESC, event_id DESC" in sql
    assert "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)" in sql


def test_refusal_says_how_old_the_standing_declaration_is(term_store, embedder, member):
    """宣言の古さは「前のセッションの置き土産」を見分ける唯一の手がかり。

    内容ではなく時刻だけを持つ — R1 の「件数のみ・内容は持たない」を破らない。
    """
    from datetime import UTC, datetime, timedelta

    registry = SessionScopeRegistry()
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    assert registry.declared_at(actor_id=member.person_id, actor_raw=None) is None
    ctx.set_session_scope("private", "私事")
    # 宣言そのものが時刻を刻むこと。ここを表明しないと、刻まなくなっても下の書式検査は
    # 注入した値で緑のままになる。
    stamped = registry.declared_at(actor_id=member.person_id, actor_raw=None)
    assert stamped is not None and (datetime.now(UTC) - stamped).total_seconds() < 60

    # TTL の内側にずらす = まだ立っている宣言
    registry._declared_at[(member.person_id, None)] = datetime.now(UTC) - timedelta(hours=5)
    refused = ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")
    assert "5時間" in refused["reason"]
    assert "自動失効" in refused["reason"]        # 残り時間も同じ息で言う
    assert "私事" not in refused["reason"]        # reason_summary は保持しない (R1)

    # 日で言う枝は TTL を伸ばした配備でしか出ないので、純関数として表明しておく。
    # 「約216時間経過」はこの機能が効く場面で最も読みにくい単位。
    from waca_loop.guard import _age_phrase as _ap

    assert "9日" in _ap(datetime.now(UTC) - timedelta(days=9))
    assert "216時間" not in _ap(datetime.now(UTC) - timedelta(days=9))

    # 1時間未満と、時計のずれ（未来の宣言時刻）。後者は「1時間以内」と**断言しない** —
    # 算出できない古さを小さい古さとして報告しないため。
    from waca_loop.guard import _age_phrase

    assert _age_phrase(datetime.now(UTC)) == "（宣言は1時間以内）"
    assert _age_phrase(datetime.now(UTC) + timedelta(hours=5)) == ""
    assert "5時間" in _age_phrase(datetime.now(UTC) - timedelta(hours=5))


def test_never_declared_subject_is_not_blocked(term_store, embedder, member):
    """宣言していない人まで巻き込まないこと — 常駐は宣言した subject にだけ効く。"""
    registry = SessionScopeRegistry()
    other = Viewer(subject="sub-other", person_id="p_other", is_org_member=True,
                   units=frozenset({"dept_eng"}))
    blocked = _ctx(term_store, embedder, member, scope_registry=registry)
    blocked.set_session_scope("external", "他組織")

    clean = _ctx(term_store, embedder, other, scope_registry=registry)
    out = clean.log_usage("l2", ["none"], "サンプル協会 テスト", "test")
    assert out.get("refused") is not True and clean.usage_store.rows


def test_declaring_exclusion_warns_that_it_outlives_the_conversation(
    term_store, embedder, member
):
    """宣言した本人に、その場で常駐を伝える。

    拒否文だけ直しても、気づくのは次の会話で記録が落ちてからになる。
    """
    ctx = _ctx(term_store, embedder, member, scope_registry=SessionScopeRegistry())
    out = ctx.set_session_scope("external", "他組織")["message"]
    assert "この会話では終わりません" in out          # 常駐すること
    assert "set_session_scope" in out                # 戻し方
    # org 側にも**同じ警告**が要る。こちらは fail-OPEN 側に効く常駐で、拒否を解除するために
    # org を宣言した人が、それが後続の会話にも残ると知らないまま私事の会話に入る。
    # 片側だけ警告するのは非対称で危ない。
    message = ctx.set_session_scope("org")["message"]
    assert "この会話では終わりません" in message
    assert "external/private" in message


def test_scope_tool_description_states_that_the_declaration_persists():
    """モデルが宣言を決めるのは description を読んだ後。常駐を書く場所として最上流。

    応答と拒否文だけ直しても、宣言する時点のモデルの理解は「この会話の話」のまま。
    """
    import waca_loop.server as srv

    src = inspect.getsource(srv.build_mcp)
    start = src.index("def set_session_scope(")
    desc = src[max(0, start - 900):start]
    assert "会話をまたいで" in desc
    assert "宣言し直す" in desc


def test_paths_that_create_the_sticky_state_warn_about_it():
    """常駐を*作る*側の2経路 (R1b/R2) にも戻し方を書く — 誤りの上流で知らせる。"""
    from waca_loop.guard import NEEDS_CONFIRMATION_MESSAGE, SCOPE_CONFIRMATION_MESSAGE

    for message in (NEEDS_CONFIRMATION_MESSAGE, SCOPE_CONFIRMATION_MESSAGE):
        assert "会話をまたいで残る" in message
        assert "org" in message


# --- R1c: 立っている宣言を、最初の書き込みを失う前に知らせる -----------------------------------


def test_policy_announces_a_standing_exclusion_before_any_write_is_lost(
    term_store, embedder, member, policy_store
):
    """拒否は最初の *書き込み* まで出ない。それまでのターンは黙って落ちる (R1c)。"""
    registry = SessionScopeRegistry()
    ctx = _ctx(term_store, embedder, member, scope_registry=registry,
               policy_store=policy_store)
    assert "scope_notice" not in ctx.get_applicable_policy()      # 平常時は黙る

    ctx.set_session_scope("private", "私事")
    notice = ctx.get_applicable_policy()["scope_notice"]
    assert "private" in notice and "set_session_scope" in notice

    ctx.set_session_scope("org")
    assert "scope_notice" not in ctx.get_applicable_policy()      # 解除したら消える


def test_scope_notice_never_blocks_the_policy(term_store, embedder, member, policy_store):
    """助言であってブロックではない — C4 と同じ制約 (§9.4)。"""
    class Exploding(SessionScopeRegistry):
        def scope_of(self, subject):
            raise RuntimeError("registry down")

    ctx = _ctx(term_store, embedder, member, scope_registry=Exploding(),
               policy_store=policy_store)
    out = ctx.get_applicable_policy()
    assert "scope_notice" not in out
    assert out["floor"] and out["legend"]        # 本体は full で返る


def test_scope_notice_is_self_only(term_store, embedder, member, steward, policy_store):
    """他 viewer の宣言状態は出さない — C4 と同じ制約。"""
    registry = SessionScopeRegistry()
    _ctx(term_store, embedder, steward, scope_registry=registry).set_session_scope(
        "private", "同僚の私事"
    )
    mine = _ctx(term_store, embedder, member, scope_registry=registry,
                policy_store=policy_store)
    assert "scope_notice" not in mine.get_applicable_policy()


def test_scope_notice_withheld_from_a_viewer_the_acl_gives_nothing(
    term_store, embedder, policy_store
):
    """ルールを教えない相手には、こちらの scope 台帳の存在も明かさない（`empty` 分岐）。"""
    outsider = Viewer(subject="s-out", person_id=None, is_org_member=False)
    registry = SessionScopeRegistry()
    ctx = _ctx(term_store, embedder, outsider, scope_registry=registry,
               policy_store=policy_store)
    ctx.set_session_scope("private", "私事")
    out = ctx.get_applicable_policy()
    assert out["legend"] == {} and "scope_notice" not in out


def test_registry_repr_does_not_dump_every_subject():
    """辞書のキーは生の OAuth sub。将来どこかで repr されても組織全員分を吐かないこと。

    宣言は session_scopes に保存されるが、メモリ上の登録簿も debug 行1本・locals を拾う
    エラー報告1つで「誰が・何を宣言し・いつ」が丸ごと出る形にはしない。
    """
    registry = SessionScopeRegistry()
    registry.declare(actor_id=None, actor_raw="hashed-12345", scope="private")
    assert "hashed-12345" not in repr(registry)
    assert registry.scope_of(actor_id=None, actor_raw="hashed-12345") == "private"


def test_age_phrase_says_nothing_rather_than_raising_on_a_naive_datetime():
    """拒否を説明する文の中で例外を投げない — 説明が仕事の文が落ちるのが一番まずい。"""
    from datetime import datetime as _dt

    from waca_loop.guard import _age_phrase

    assert _age_phrase(_dt(2026, 8, 1, 12, 0)) == ""          # tz なし


# --- TTL と永続化------------------------------------------------------


def test_a_lapse_does_not_resume_recording_on_its_own(term_store, embedder, member):
    """失効は宣言の効力を切るが、記録を再開はしない。

    失効した瞬間に記録を再開すると、失敗方向が高くつく — まだ業務外の会話が、本人が
    何もしていないのに台帳へ入る。失効しても拒否のままで、どちらであるかを人が1回言う
    まで動かない。
    """
    from datetime import UTC, datetime, timedelta

    registry = SessionScopeRegistry(ttl_hours=12)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    ctx.set_session_scope("private", "私事")
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["refused"] is True

    registry._declared_at[(member.person_id, None)] = datetime.now(UTC) - timedelta(hours=13)
    out = ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")
    assert out["refused"] is True and not ctx.usage_store.rows
    # 効力は切れている（scope_of は unknown）が、書き込みは止まったまま
    assert registry.scope_of(actor_id=member.person_id, actor_raw=None) == "unknown"
    assert registry.is_lapsed(actor_id=member.person_id, actor_raw=None) is True
    assert registry.writes_blocked(actor_id=member.person_id, actor_raw=None) is True


def test_an_explicit_declaration_is_what_resumes_recording(term_store, embedder, member):
    """再開の唯一の経路は明示の宣言。時間の経過ではない。"""
    from datetime import UTC, datetime, timedelta

    registry = SessionScopeRegistry(ttl_hours=12)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    ctx.set_session_scope("private", "私事")
    registry._declared_at[(member.person_id, None)] = datetime.now(UTC) - timedelta(hours=13)
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["refused"] is True

    ctx.set_session_scope("org", "業務に戻った")
    out = ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")
    assert out.get("refused") is not True and ctx.usage_store.rows
    assert registry.is_lapsed(actor_id=member.person_id, actor_raw=None) is False


def test_re_declaring_the_same_exclusion_extends_it(term_store, embedder, member):
    """まだ業務外なら、同じ scope を宣言し直せば失効状態から普通の除外へ戻る。"""
    from datetime import UTC, datetime, timedelta

    registry = SessionScopeRegistry(ttl_hours=12)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    ctx.set_session_scope("private", "私事")
    registry._declared_at[(member.person_id, None)] = datetime.now(UTC) - timedelta(hours=13)
    assert registry.is_lapsed(actor_id=member.person_id, actor_raw=None) is True

    ctx.set_session_scope("private", "まだ私事")
    assert registry.is_lapsed(actor_id=member.person_id, actor_raw=None) is False
    assert registry.writes_blocked(actor_id=member.person_id, actor_raw=None) is True
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["refused"] is True


def test_the_refusal_after_a_lapse_names_what_was_declared(term_store, embedder, member):
    """失効中の拒否が scope=unknown を名乗ると、読み手には不具合の形に見える。"""
    from datetime import UTC, datetime, timedelta

    registry = SessionScopeRegistry(ttl_hours=12)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    ctx.set_session_scope("private", "私事")
    registry._declared_at[(member.person_id, None)] = datetime.now(UTC) - timedelta(hours=13)

    out = ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")
    assert out["scope"] == "private" and out["scope_state"] == "lapsed"
    assert "自動失効" in out["reason"] and "再開しません" in out["reason"]
    assert "org" in out["reason"]          # 戻し方が書いてある
    # 立っている宣言との取り違えが起きないこと（同じ名前で bool と文章を返さない）
    ctx.set_session_scope("private", "まだ私事")
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["scope_state"] == "standing"


def test_expiry_is_announced_before_it_happens(term_store, embedder, member, policy_store):
    """失効の失敗方向は高くつく（私事が台帳に入り、本人が気づいて取り下げない限り残る）。黙って切り替えない。"""
    from datetime import UTC, datetime, timedelta

    registry = SessionScopeRegistry(ttl_hours=12)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry,
               policy_store=policy_store)
    ctx.set_session_scope("private", "私事")
    assert "scope_expiry_warning" not in ctx.get_applicable_policy()   # まだ先なら黙る

    registry._declared_at[(member.person_id, None)] = datetime.now(UTC) - timedelta(hours=11)
    warning = ctx.get_applicable_policy()["scope_expiry_warning"]
    assert "自動失効" in warning and "set_session_scope" in warning

    # 失効後は警告を出し続けない。ただし失効後も**まだ止まっている**ので、理由の掲示は残る
    # — ここを消すと「拒否されているのに画面に理由が無い」という最悪の形になる。
    registry._declared_at[(member.person_id, None)] = datetime.now(UTC) - timedelta(hours=13)
    after = ctx.get_applicable_policy()
    assert "scope_expiry_warning" not in after
    assert "自動失効" in after["scope_notice"] and "再開しません" in after["scope_notice"]


def test_unknown_declaration_age_never_expires(term_store, embedder, member):
    """時刻を持たない宣言は失効させない — 古さが分からないものを「古い」と扱わない。

    宣言はしたのに時刻を持たない行（時刻を持たない別実装など）を失効扱いに
    すると、業務外と宣言した人の記録が本人に無断で再開する。分からない側は締める。
    """
    from waca_loop.guard import scope_expired

    assert scope_expired(None, ttl_hours=12) is False

    registry = SessionScopeRegistry(ttl_hours=12)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    ctx.set_session_scope("private", "私事")
    del registry._declared_at[(member.person_id, None)]              # 時刻だけ失う
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["refused"] is True


def test_ttl_zero_restores_the_permanent_behaviour(term_store, embedder, member):
    """配備側の逃げ道。コード変更なしで「宣言し直すまで永続」の動作にできる。"""
    from datetime import UTC, datetime, timedelta

    from waca_loop.guard import scope_expired

    assert scope_expired(datetime.now(UTC) - timedelta(days=365), ttl_hours=0) is False

    registry = SessionScopeRegistry(ttl_hours=0)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    ctx.set_session_scope("private", "私事")
    registry._declared_at[(member.person_id, None)] = datetime.now(UTC) - timedelta(days=30)
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["refused"] is True


def test_declaration_survives_a_restart(term_store, embedder, member):
    """永続化の本体。再起動が黙った失効理由にならないこと。

    永続化しなければ、再デプロイのたびに全員の宣言が unknown へ戻る。
    """
    from waca_loop.ledger.session_scopes import InMemorySessionScopeStore

    store = InMemorySessionScopeStore()
    before = _ctx(term_store, embedder, member,
                  scope_registry=SessionScopeRegistry(store=store))
    before.set_session_scope("private", "私事")

    # 別プロセス相当 — レジストリを作り直す。ストアだけが渡る
    after = _ctx(term_store, embedder, member,
                 scope_registry=SessionScopeRegistry(store=store))
    assert after.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["refused"] is True
    assert after.usage_store.rows == []


def test_persisted_declarations_are_keyed_by_actor_not_raw_subject(
    term_store, embedder, member
):
    """§5: 保存する鍵は生の sub ではない。ここが生 sub だと保存の時点で規約違反になる。"""
    from waca_loop.ledger.session_scopes import InMemorySessionScopeStore

    store = InMemorySessionScopeStore()
    ctx = _ctx(term_store, embedder, member, scope_registry=SessionScopeRegistry(store=store))
    ctx.set_session_scope("private", "私事")

    (key,) = store.rows
    assert key == (member.person_id, None)
    assert member.subject not in str(store.rows)        # 生 sub はどこにも無い
    assert "私事" not in str(store.rows)                 # 理由も保存しない (R1)


def test_a_store_outage_does_not_unblock_an_exclusion(term_store, embedder, member):
    """ストアが落ちても、立っている除外を勝手に解除しない（fail-open にしない）。"""
    class Exploding:
        def upsert(self, **kw):
            raise RuntimeError("store down")

        def get(self, **kw):
            raise RuntimeError("store down")

    registry = SessionScopeRegistry(store=Exploding())
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    # 宣言は通る（保存に失敗してもプロセス内では効いている）
    assert ctx.set_session_scope("private", "私事")["scope"] == "private"
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["refused"] is True


def test_re_declaring_soon_after_does_not_lose_the_new_scope(term_store, embedder, member):
    """BigQuery は streaming buffer の行への DML を拒む（~30〜90分）。

    そして**その窓の中で宣言し直すのが普通の使い方**（拒否文自身がそう案内する）。
    DELETE+INSERT だと DELETE が落ちて INSERT が走らず、ストアに古い方の scope が残り、
    再起動で除外が消える。追記なら起きない。
    """
    from waca_loop.ledger.session_scopes import InMemorySessionScopeStore

    store = InMemorySessionScopeStore()
    ctx = _ctx(term_store, embedder, member, scope_registry=SessionScopeRegistry(store=store))
    ctx.set_session_scope("org")
    ctx.set_session_scope("private", "私事")        # 直後に切り替える

    restarted = _ctx(term_store, embedder, member,
                     scope_registry=SessionScopeRegistry(store=store))
    assert restarted.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["refused"] is True


def test_a_write_failure_is_counted_and_never_silent(term_store, embedder, member):
    """保存に失敗したら数える。**これが上がっていることだけが**、永続化が効いていない
    （DDL 未適用を含む）唯一の signal になる。"""
    class Exploding:
        def upsert(self, **kw):
            raise RuntimeError("store down")

        def get(self, **kw):
            return None

    registry = SessionScopeRegistry(store=Exploding())
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    ctx.set_session_scope("private", "私事")
    assert registry.store_write_failures == 1
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["refused"] is True


def test_a_cold_read_failure_is_counted_and_not_cached(term_store, embedder, member):
    """★ 読み取り失敗は fail-OPEN に degrade する。**それを隠さない**こと。

    再起動＋BQ 障害（テーブル未作成を含む）では、立っていた除外が消える。全員の書き込みを
    止めるほうが害が大きいのでそう倒しているが、数え・記録し、次回は読み直す。
    """
    class Exploding:
        def upsert(self, **kw):
            pass

        def get(self, **kw):
            raise RuntimeError("store down")

    registry = SessionScopeRegistry(store=Exploding())
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)   # キャッシュは空＝冷えた状態
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test").get("refused") is not True
    assert registry.store_read_failures >= 1
    before = registry.store_read_failures
    ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")
    assert registry.store_read_failures > before      # 失敗はキャッシュしない＝次回も読む


def test_a_lapse_announces_itself_once(term_store, embedder, member, policy_store):
    """★ 失効の**再開側**も知らせる。事前警告は寝ている間に過ぎうる。"""
    from datetime import UTC, datetime, timedelta

    registry = SessionScopeRegistry(ttl_hours=12)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry,
               policy_store=policy_store)
    ctx.set_session_scope("private", "私事")
    registry._declared_at[(member.person_id, None)] = datetime.now(UTC) - timedelta(hours=13)

    notice = ctx.get_applicable_policy()["scope_lapsed"]
    assert "失効" in notice and "private" in notice
    assert "再開していません" in notice          # 再開の予告ではなく、止まっている旨
    assert "scope_lapsed" not in ctx.get_applicable_policy()     # 2回目は出さない


def test_declaration_time_message_states_the_ttl(term_store, embedder, member):
    """寿命は**決めた時点で**言う。あとから拒否文で知らせる形だと理解が古いままになる。"""
    ctx = _ctx(term_store, embedder, member,
               scope_registry=SessionScopeRegistry(ttl_hours=12))
    msg = ctx.set_session_scope("external", "他組織")["message"]
    assert "12時間" in msg and "自動失効" in msg
    # 失効＝再開ではない。決めた時点でそう言う（あとで拒否文に驚かせない）。
    assert "自動では再開しません" in msg and "org" in msg

    forever = _ctx(term_store, embedder, member,
                   scope_registry=SessionScopeRegistry(ttl_hours=0))
    assert "終わりません" in forever.set_session_scope("external", "他組織")["message"]


def test_a_stale_cache_is_re_read(term_store, embedder, member):
    """キャッシュは無期限ではない。手で行を消した／別インスタンスが宣言した、が届くこと。"""
    from waca_loop.ledger.session_scopes import InMemorySessionScopeStore

    store = InMemorySessionScopeStore()
    registry = SessionScopeRegistry(store=store, cache_seconds=0)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test").get("refused") is not True

    store.upsert(actor_id=member.person_id, actor_raw=None, scope="private")
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["refused"] is True


def test_the_registry_and_the_store_agree_on_who_an_actor_is(term_store, embedder, member):
    """「同じ actor」の定義が2つあると、両方渡された瞬間だけ別の宣言になる。"""
    from waca_loop.guard import scope_key
    from waca_loop.ledger.session_scopes import InMemorySessionScopeStore

    assert scope_key("p", "h") == scope_key("p", None) == ("p", None)

    store = InMemorySessionScopeStore()
    registry = SessionScopeRegistry(store=store)
    registry.declare(actor_id="p", actor_raw="h", scope="private")
    assert registry.scope_of(actor_id="p", actor_raw=None) == "private"
    assert store.get(actor_id="p", actor_raw=None)[0] == "private"
    (row,) = store.rows.values()
    assert row["actor_raw"] is None      # 行が両方を持たない


# --- 失効と読み取り失敗の境界 ----------------------------------------------


class _UnreadableScopeStore:
    """読み取りだけ落ちるストア。書き込みは通る（BigQuery の 503 / DDL 未適用に近い形）。"""

    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert(self, *, actor_id=None, actor_raw=None, scope="unknown", declared_at=None):
        self.rows[(actor_id, actor_raw)] = scope

    def get(self, *, actor_id=None, actor_raw=None):
        raise RuntimeError("BigQuery unavailable")


def test_an_unverifiable_scope_records_as_pending_not_as_a_guess(
    term_store, embedder, member
):
    """スコープを読めないとき「宣言は無い」と決めつけない。

    止めるべき集合には「失効済み（無期限）」も含まれるので、fail-open にすると読み取り失敗
    1回の被害範囲が大きい。かといって拒否するとログが失われる。`pending` はどちらでもない —
    行は残り、    recorded として数えられず、スチュワードが承認か物理削除を選べる。
    """
    registry = SessionScopeRegistry(store=_UnreadableScopeStore(), cache_seconds=0)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)

    out = ctx.log_usage("l2", ["none"], "英文メールの言い回しを3案に整えた", "test")
    assert out.get("refused") is not True            # 拒否ではない（ログは失わない）
    assert out["status"] == "needs_confirmation"
    assert out["scope_state"] == "unverified"
    assert "確認できなかった" in out["message"]      # R2 の「サンプル協会と無関係」とは別の文言
    assert [r["status"] for r in ctx.usage_store.rows] == ["pending"]
    assert registry.store_read_failures > 0


def test_a_readable_scope_still_records_normally(term_store, embedder, member):
    """上の裏返し。読めているときに pending へ落とさない（保留の乱発は台帳を無意味にする）。"""
    from waca_loop.ledger.session_scopes import InMemorySessionScopeStore

    registry = SessionScopeRegistry(store=InMemorySessionScopeStore(), cache_seconds=0)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    out = ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")
    assert out.get("status") != "needs_confirmation"
    assert [r["status"] for r in ctx.usage_store.rows] == ["recorded"]


def test_a_row_dropped_by_retention_announces_itself(
    term_store, embedder, member, policy_store
):
    """90日のパーティション失効は「時間による再開」の第2の経路。黙らせない。

    「再開は宣言だけ」という性質を、この経路が黙って破らないようにする。保持期間そのものは変えない（行は人に
    貼られるラベルとして読める種類の記録）— 変えるのは、消えたことが黙って起きないこと。
    """
    from waca_loop.ledger.session_scopes import InMemorySessionScopeStore

    store = InMemorySessionScopeStore()
    registry = SessionScopeRegistry(store=store, cache_seconds=0)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry,
               policy_store=policy_store)
    ctx.set_session_scope("private", "私事")
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["refused"] is True

    store.rows.clear()                      # 保持期間切れでパーティションが落ちた状態
    notice = ctx.get_applicable_policy()["scope_vanished"]
    assert "90日" in notice and "再開されています" in notice and "private" in notice
    assert "scope_vanished" not in ctx.get_applicable_policy()   # 2回目は出さない


def test_unknown_cannot_be_used_to_clear_a_lapse(term_store, embedder, member):
    """「わからない」は明示の答えではない。"""
    from datetime import UTC, datetime, timedelta

    registry = SessionScopeRegistry(ttl_hours=12)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    ctx.set_session_scope("private", "私事")
    registry._declared_at[(member.person_id, None)] = datetime.now(UTC) - timedelta(hours=13)

    with pytest.raises(ValueError, match="unknown"):
        ctx.set_session_scope("unknown", "判断がつかない")
    assert registry.writes_blocked(actor_id=member.person_id, actor_raw=None) is True

    # 立っていない相手には通常どおり通る（unknown 自体を禁じたわけではない）
    other = SessionScopeRegistry(ttl_hours=12)
    _ctx(term_store, embedder, member, scope_registry=other).set_session_scope("unknown", None)
    assert other.scope_of(actor_id=member.person_id, actor_raw=None) == "unknown"


def test_a_member_with_no_policy_rows_still_learns_why_writes_are_refused(
    term_store, embedder, member
):
    """ポリシー行ゼロの分岐でも、一度きりの失効通知を捨てずに届けること。

    `empty` を踏むのは部外者だけではない — 行の無い部署、まだ approved でない行でも踏む。
    失効後の拒否は無期限なので、そこで通知を捨てると「理由の出ない永久ブロック」になる。
    """
    from datetime import UTC, datetime, timedelta

    registry = SessionScopeRegistry(ttl_hours=12)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)   # policy_store なし
    ctx.set_session_scope("private", "私事")
    registry._declared_at[(member.person_id, None)] = datetime.now(UTC) - timedelta(hours=13)

    out = ctx.get_applicable_policy()
    assert out["legend"] == {}                       # 語彙は渡さないまま
    assert "自動失効" in out["scope_notice"]         # 理由は渡す
    assert "再開していません" in out["scope_lapsed"]


def test_a_registry_without_is_lapsed_degrades_instead_of_dying(
    term_store, embedder, member, policy_store
):
    """片方が黙り片方が例外、という食い違いを作らない（scope_registry は duck-typed）。"""

    class _OldRegistry:
        def __init__(self):
            self.scope = "private"

        def scope_of(self, **_kw):
            return self.scope

        def declared_at(self, **_kw):
            return None

        def expires_in_hours(self, **_kw):
            return None

        def writes_blocked(self, **_kw):
            return True

    ctx = _ctx(term_store, embedder, member, scope_registry=_OldRegistry(),
               policy_store=policy_store)
    # 落ちない
    assert ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["refused"] is True
    assert "scope_notice" in ctx.get_applicable_policy()                              # 黙らない


def test_the_lapsed_refusal_does_not_repeat_how_old_the_declaration_is(
    term_store, embedder, member
):
    """失効は無期限なので、日数を出すと毎ターン無期限に個人の宣言歴を掲示することになる。"""
    from datetime import UTC, datetime, timedelta

    registry = SessionScopeRegistry(ttl_hours=12)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry)
    ctx.set_session_scope("private", "私事")
    registry._declared_at[(member.person_id, None)] = datetime.now(UTC) - timedelta(days=30)

    reason = ctx.log_usage("l2", ["none"], "サンプル協会 テスト", "test")["reason"]
    assert "自動失効" in reason
    assert "日経過" not in reason and "時間経過" not in reason


def test_a_declaration_arriving_through_the_store_re_arms_the_lapse_notice(
    term_store, embedder, member, policy_store
):
    """行を手で直した場合も通知が復活すること。"""
    from datetime import UTC, datetime, timedelta

    from waca_loop.ledger.session_scopes import InMemorySessionScopeStore

    store = InMemorySessionScopeStore()
    registry = SessionScopeRegistry(store=store, ttl_hours=12, cache_seconds=0)
    ctx = _ctx(term_store, embedder, member, scope_registry=registry,
               policy_store=policy_store)
    ctx.set_session_scope("private", "私事")
    old = datetime.now(UTC) - timedelta(hours=13)
    store.upsert(actor_id=member.person_id, actor_raw=None, scope="private", declared_at=old)
    registry._declared_at[(member.person_id, None)] = old
    assert "scope_lapsed" in ctx.get_applicable_policy()          # 1回目
    assert "scope_lapsed" not in ctx.get_applicable_policy()      # 2回目は出さない

    # 別インスタンス／手作業で**より新しい**行が入った → 通知は再び武装される
    # （12.5時間前なので TTL 12時間はなお超えており、失効状態のまま）
    store.upsert(actor_id=member.person_id, actor_raw=None, scope="private",
                 declared_at=datetime.now(UTC) - timedelta(hours=12, minutes=30))
    assert "scope_lapsed" in ctx.get_applicable_policy()
