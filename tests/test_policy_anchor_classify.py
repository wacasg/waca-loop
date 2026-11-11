"""Policy anchoring + advisory classification on log_usage (SPEC §9.3/§9.4, P1–P5).

Advisory only by design: every case here also asserts the ledger write succeeded —
warnings and mismatches never block the record (統治の負荷はリスクに比例・責めない).
"""

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


def _kyozai() -> Viewer:
    return Viewer(
        subject="s", person_id="p", is_org_member=True, units=frozenset({"kyozai"})
    )


def test_p1_anchor_recorded_and_optional(term_store, embedder, policy_store):
    """P1: policy_row_ids reach the ledger with the applicable version; omitting them gives []."""
    ctx = _ctx(term_store, embedder, _kyozai(), policy_store)

    ctx.log_usage("l1", ["confidential"], "受講データ分析", "claude_code",
                  policy_row_ids=["KYO-06", "FLOOR-01"])
    ctx.log_usage("l2", ["public"], "議事メモ整形", "claude_code")

    anchored, plain = ctx.usage_store.rows
    assert anchored["policy_row_ids"] == ["KYO-06", "FLOOR-01"]
    assert anchored["policy_version"] == 1          # max version of the applicable rows
    # Unaware clients: zero friction — omitted anchors become [] in the row, never None.
    assert plain["policy_row_ids"] == []
    assert plain["policy_version"] == 1             # version still stamped (rules applied)


def test_p2_floor_warning_with_detour_never_blocks(term_store, embedder, policy_store):
    """P2: pii fires the 下限 — detour surfaced, record still written."""
    ctx = _ctx(term_store, embedder, _kyozai(), policy_store)
    out = ctx.log_usage("l0", ["pii"], "会員名簿の照合", "chatgpt")

    assert out["log_id"] and len(ctx.usage_store.rows) == 1   # recorded, not blocked
    (warning,) = out["floor_warnings"]
    assert warning["row_id"] == "FLOOR-01"
    assert "安全チャネル" in warning["detour"]                 # 迂回路 reaches the user
    assert out["suggested_category"] == "prohibited"           # 下限 = 禁止列挙
    assert out["mismatch"] is True                             # l0 declared, 下限 is heavier


def test_p3_mismatch_flag_ledger_keeps_declared(term_store, embedder, policy_store):
    """P3: l2-declared pii → mismatch flagged; the ledger keeps the declared value as-is."""
    ctx = _ctx(term_store, embedder, _kyozai(), policy_store)
    out = ctx.log_usage("l2", ["pii"], "顧客リスト要約", "claude_code")

    assert out["mismatch"] is True and out["suggested_category"] == "prohibited"
    (row,) = ctx.usage_store.rows
    assert row["use_category"] == "l2"                         # 申告値を尊重（責めない）


def test_p4_no_trigger_no_noise(term_store, embedder, policy_store):
    """P4: kinds touching no trigger produce no suggestion, no warnings, no mismatch."""
    ctx = _ctx(term_store, embedder, _kyozai(), policy_store)
    out = ctx.log_usage("l2", ["public", "internal"], "公開情報の要約", "claude_code")

    assert out["suggested_category"] is None
    assert out["mismatch"] is False and out["floor_warnings"] == []


def test_p5_other_dept_triggers_do_not_fire(term_store, embedder, policy_store):
    """P5: KYO-06's trigger (confidential) is invisible outside kyozai — ACL double gate."""
    members = Viewer(
        subject="s2", person_id="p2", is_org_member=True, units=frozenset({"members"})
    )
    out = _ctx(term_store, embedder, members, policy_store).log_usage(
        "l2", ["confidential"], "社内資料の下書き", "claude_code"
    )
    assert out["suggested_category"] is None                      # dept rule did not leak

    kyozai_out = _ctx(term_store, embedder, _kyozai(), policy_store).log_usage(
        "l2", ["confidential"], "社内資料の下書き", "claude_code"
    )
    assert kyozai_out["suggested_category"] == "l1"            # same kinds, own dept → fires
    assert kyozai_out["floor_warnings"] == []                  # L1 is not a 下限 warning


def test_response_keeps_its_shape_without_policy_store(term_store, embedder):
    """No policy store configured → advisory keys present but empty (same response shape)."""
    out = _ctx(term_store, embedder, Viewer.anonymous("x")).log_usage(
        "l2", ["public"], "s", "t"
    )
    assert out["log_id"]
    assert out["suggested_category"] is None
    assert out["mismatch"] is False and out["floor_warnings"] == []


def test_p6_level_mapping_to_ledger_vocabulary(term_store, embedder, policy_store):
    """§9.4 level→use_category写像: level0→l0（下限でなくても）, level2→l2, 未設定→l1."""
    from waca_loop.models import PolicyRule

    policy_store.add(PolicyRule(
        row_id="KYO-90", scope_unit="kyozai", level=0,
        line="この行為はAI未使用（差し控え）", effect="人手で実施",
        trigger_kinds=["raw_export"],
    ))
    policy_store.add(PolicyRule(
        row_id="KYO-91", scope_unit="kyozai", level=2,
        line="議事メモの整形", effect="", trigger_kinds=["meeting_notes"],
    ))
    policy_store.add(PolicyRule(
        row_id="KYO-92", scope_unit="kyozai", level=None,
        line="level未設定の業務行", effect="", trigger_kinds=["unlabeled_kind"],
    ))
    ctx = _ctx(term_store, embedder, _kyozai(), policy_store)

    # level0 fires → l0, WITHOUT any 下限 warning (severity raised by 未使用, not a 下限).
    out0 = ctx.log_usage("l2", ["raw_export"], "生データの持ち出し検討", "claude_code")
    assert out0["suggested_category"] == "l0" and out0["floor_warnings"] == []
    assert out0["mismatch"] is True                            # declared l2 < suggested l0

    # level2 fires → l2; declaring l2 is not a mismatch.
    out2 = ctx.log_usage("l2", ["meeting_notes"], "議事メモ整形", "claude_code")
    assert out2["suggested_category"] == "l2" and out2["mismatch"] is False

    # Unlabeled business row fires → conservative l1 (same default as delivery's level1).
    outn = ctx.log_usage("l2", ["unlabeled_kind"], "未設定行の作業", "claude_code")
    assert outn["suggested_category"] == "l1" and outn["mismatch"] is True


def test_p7_prohibited_declared_is_not_a_mismatch(term_store, embedder, policy_store):
    """Vocabulary: 下限 fires → suggestion is ``prohibited``; declaring it is honest.

    ``prohibited`` is a separate, heaviest bucket: if 「未使用」(level 0) and 「禁止列挙に
    該当」 shared one value, a user who correctly declared a floor case would look identical
    to a level-0 one in the ledger.
    """
    ctx = _ctx(term_store, embedder, _kyozai(), policy_store)
    out = ctx.log_usage("prohibited", ["pii"], "会員名簿の照合", "chatgpt")

    assert out["suggested_category"] == "prohibited"
    assert out["mismatch"] is False                            # declared honestly
    assert out["floor_warnings"][0]["row_id"] == "FLOOR-01"    # detour still surfaced
    (row,) = ctx.usage_store.rows
    assert row["use_category"] == "prohibited"                 # recorded, never blocked
