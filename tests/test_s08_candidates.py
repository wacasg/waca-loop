"""S8 — 抽出候補: AI提案を利用者が承認すると用語がグラウンディング層へ昇格.

Covers the improvement-loop mechanism: extract term/SFAQ/ERN candidates
with a proposed sharing scope, then AI提案＋利用者承認.
"""

from __future__ import annotations

from waca_loop.retrieval.hybrid import hybrid_search


def _search(ctx, embedder, viewer, query):
    return hybrid_search(query, viewer, 5, store=ctx.term_store, embedder=embedder)


def test_propose_then_approve_promotes_term(steward_context, embedder, viewer_member):
    """用語の昇格は組織語彙の書き換え = スチュワード専用。"""
    out = steward_context.propose_candidates(
        [
            {
                "kind": "term",
                "payload": {"term": "CVR", "definition": "コンバージョン率"},
                "scope": {"tier": "dept", "audience": "internal", "sensitivity": "normal"},
                "circles": ["analytics"],
                "owner_unit": "dept_eng",
                "source": "prompt",
            }
        ]
    )
    cid = out["candidate_ids"][0]

    # Not searchable before approval.
    before = _search(steward_context, embedder, viewer_member, "CVR")
    assert all(h.term != "CVR" for h in before)

    res = steward_context.approve_candidate(cid, approve=True)
    assert res["status"] == "approved"
    assert res["promoted"] is True

    after = _search(steward_context, embedder, viewer_member, "CVR")
    assert any(h.term == "CVR" for h in after), "approved term should be searchable"


def test_term_promotion_is_steward_only(survey_context, embedder):
    """C(負): 一般メンバーは組織語彙を書き換えられない."""
    import pytest

    out = survey_context.propose_candidates(
        [{"kind": "term", "payload": {"term": "PPP", "definition": "勝手な定義"},
          "owner_unit": "dept_eng"}]
    )
    with pytest.raises(PermissionError, match="steward"):
        survey_context.approve_candidate(out["candidate_ids"][0], approve=True)
    assert all(h.term != "PPP" for h in _search(survey_context, embedder,
                                                survey_context.viewer, "PPP"))


def test_org_tier_proposal_is_steward_only(survey_context):
    """C(負): 全社スコープの提案自体がスチュワード判断 — 既定は dept."""
    import pytest

    with pytest.raises(PermissionError, match="org-tier"):
        survey_context.propose_candidates(
            [{"kind": "term", "payload": {"term": "QQQ", "definition": "x"},
              "scope": {"tier": "org", "audience": "internal", "sensitivity": "normal"}}]
        )


def test_proposal_cannot_be_filed_into_another_unit(survey_context):
    """C(負): 所属していないユニットの候補は出せない(昇格時にそのまま写されるため)."""
    import pytest

    with pytest.raises(PermissionError):
        survey_context.propose_candidates(
            [{"kind": "term", "payload": {"term": "RRR", "definition": "x"},
              "owner_unit": "secretariat"}]   # viewer_member は dept_eng
        )


def test_term_gate_does_not_depend_on_whether_a_sink_is_wired(embedder, viewer_member):
    """C(負): 昇格ゲートは候補の種別で決まる — シンクの有無で外れない.

    シンクの有無でゲートを決めると、`add` を持たないストアを差した構成だけ**無認可**に
    なる。`BigQueryTermStore` は `add` を持つが、
    それでゲートが「たまたま」効いているのでは同じ穴が残る — add を持たないストアでも
    拒否されることを、ここで独立に固定する。
    """
    import pytest

    from waca_loop.grounding.terms import BigQueryTermStore
    from waca_loop.improvement.candidates import InMemoryCandidateStore
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore
    from waca_loop.server import ServerContext

    # 本番ストアは書ける(承認の行き止まりを塞いでいる)。ゲートの検証は
    # それとは無関係に成り立つ、というのがこのテストの主張。
    assert hasattr(BigQueryTermStore, "add")

    class _NoAddStore:            # 本番ストアと同じ「add を持たない」形
        def lexical_candidates(self, *a, **k): return []
        def semantic_candidates(self, *a, **k): return []

    ctx = ServerContext(
        term_store=_NoAddStore(), usage_store=InMemoryUsageLogStore(), embedder=embedder,
        viewer=viewer_member, candidate_store=InMemoryCandidateStore(),
    )
    out = ctx.propose_candidates(
        [{"kind": "term", "payload": {"term": "SSS", "definition": "x"},
          "owner_unit": "dept_eng"}]
    )
    with pytest.raises(PermissionError, match="steward"):
        ctx.approve_candidate(out["candidate_ids"][0], approve=True)


def test_term_reapproval_does_not_mint_a_second_row(steward_context, embedder, viewer_member):
    """C(負): 用語候補の再承認で語彙が二重に増えない."""
    out = steward_context.propose_candidates(
        [{"kind": "term", "payload": {"term": "TTT", "definition": "一度だけ"},
          "owner_unit": "dept_eng"}]
    )
    cid = out["candidate_ids"][0]
    assert steward_context.approve_candidate(cid, approve=True)["promoted"] is True
    assert steward_context.approve_candidate(cid, approve=True)["promoted"] is False
    assert len([h for h in _search(steward_context, embedder, viewer_member, "TTT")
                if h.term == "TTT"]) == 1


def test_promoted_term_is_never_public(steward_context, embedder):
    """C: 提案が public と言っても、昇格した用語の audience は internal に固定される."""
    from waca_loop.models import ScopeAudience, Viewer

    out = steward_context.propose_candidates(
        [{"kind": "term", "payload": {"term": "UUU", "definition": "外に出さない"},
          "scope": {"tier": "dept", "audience": "public", "sensitivity": "normal"},
          "owner_unit": "dept_eng"}]
    )
    steward_context.approve_candidate(out["candidate_ids"][0], approve=True)

    outsider = Viewer.anonymous(subject="sub_hashed")
    assert all(h.term != "UUU" for h in _search(steward_context, embedder, outsider, "UUU"))
    promoted = [h for h in _search(steward_context, embedder, steward_context.viewer, "UUU")
                if h.term == "UUU"]
    assert promoted and promoted[0].scope.audience is ScopeAudience.INTERNAL


def test_shared_layer_writes_require_a_resolved_identity(term_store, embedder):
    """C(負): 身元未解決のビューアは共有層へ書けない — ホスト配線任せ(片肺)をやめる."""
    import pytest

    from waca_loop.improvement.candidates import InMemoryCandidateStore
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore
    from waca_loop.models import Viewer
    from waca_loop.server import ServerContext

    ghost = ServerContext(
        term_store=term_store, usage_store=InMemoryUsageLogStore(), embedder=embedder,
        viewer=Viewer.anonymous(subject="sub_hashed"),
        candidate_store=InMemoryCandidateStore(),
    )
    with pytest.raises(PermissionError, match="identity"):
        ghost.propose_candidates([{"kind": "term", "payload": {"term": "X", "definition": "y"}}])


def test_reject_does_not_promote(steward_context, embedder, viewer_member):
    """却下しても昇格しないこと。**却下できるのはスチュワードだけ**.

    非スチュワードに却下を許すと、「決定は終局」のもとでは非スチュワードが
    共有語彙への提案に **durable な拒否権**を持つことになる
    （却下後はスチュワードが承認しようとしても `candidate already rejected` で落ちる）。
    """
    unit = sorted(steward_context.viewer.units)[0]
    out = steward_context.propose_candidates(
        [{"kind": "term", "payload": {"term": "ZZZ", "definition": "x"}, "owner_unit": unit}]
    )
    res = steward_context.approve_candidate(out["candidate_ids"][0], approve=False)
    assert res["status"] == "rejected"
    assert res["promoted"] is False

    hits = _search(steward_context, embedder, viewer_member, "ZZZ")
    assert all(h.term != "ZZZ" for h in hits)


def test_a_member_cannot_veto_a_term_candidate_by_rejecting_it(steward_context):
    """非スチュワードの却下で、共有語彙への提案が永久に潰れないこと.

    単体では妥当な2つの規則——「決定は終局」と「却下ゲートは SFAQ だけ」——は、
    合わさると穴になる。用語候補を誰でも却下でき、却下は終局なので、
    後からスチュワードが承認できなくなる。
    """
    import pytest

    unit = sorted(steward_context.viewer.units)[0]
    cid = steward_context.propose_candidates(
        [{"kind": "term", "payload": {"term": "VETO", "definition": "x"}, "owner_unit": unit}]
    )["candidate_ids"][0]

    steward_context.viewer = _member()
    with pytest.raises(PermissionError, match="reject"):
        steward_context.approve_candidate(cid, approve=False)


def test_sfaq_candidate_approved_but_not_promoted_to_terms(steward_context):
    out = steward_context.propose_candidates(
        [{"kind": "sfaq", "payload": {"question": "返品は？", "decision": "14日以内可"},
          "owner_unit": "dept_eng"}]
    )
    res = steward_context.approve_candidate(out["candidate_ids"][0], approve=True)
    assert res["status"] == "approved"
    assert res["promoted"] is False  # SFAQ goes to its own pipeline (SPEC_phase2), not terms


def test_sfaq_candidate_approval_promotes_to_provisional_question(
    term_store, person_store, embedder
):
    """S8 — approve on an SFAQ candidate creates a provisional Question that keeps the
    proposed scope verbatim (sensitive is NOT widened to normal) and carries the
    judgment as first feedback. The approve act is the human gate (§2.4)."""
    from waca_loop.embeddings import HashingEmbedder
    from waca_loop.improvement.candidates import InMemoryCandidateStore
    from waca_loop.knowledge.store import InMemoryQuestionStore
    from waca_loop.models import Viewer
    from waca_loop.server import ServerContext

    steward = Viewer(
        subject="s3", person_id="p_tanaka", is_org_member=True,
        units=frozenset({"steward", "secretariat"}),
    )
    kstore = InMemoryQuestionStore([], HashingEmbedder(dim=64))
    ctx = ServerContext(
        term_store=term_store,
        usage_store=__import__(
            "waca_loop.ledger.usage_log", fromlist=["InMemoryUsageLogStore"]
        ).InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=steward,
        person_store=person_store,
        candidate_store=InMemoryCandidateStore(),
        knowledge_store=kstore,
    )

    out = ctx.propose_candidates(
        [
            {
                "kind": "sfaq",
                "payload": {
                    "question_text": "会報の送付先変更は電話でも受け付けるか?",
                    "judgment": "本人確認のうえ受け付ける",
                    "source_refs": ["rec-cc-0123456789abcdef"],
                    "support_count": 4,
                },
                "scope": {
                    "tier": "dept", "audience": "internal", "sensitivity": "sensitive",
                },
                "owner_unit": "secretariat",
                "source": "cc_promote",
            }
        ]
    )
    res = ctx.approve_candidate(out["candidate_ids"][0], approve=True)
    assert res["status"] == "approved"
    assert res["promoted"] is True
    qid = res["question_id"]

    q = next(qq for qq in kstore._questions if qq.question_id == qid)  # noqa: SLF001
    assert q.status.value == "provisional", "approval is the human gate -> live with label"
    # Scope carried verbatim — the add_sfaq_question org/normal default must NOT apply.
    assert (q.scope.tier.value, q.scope.audience.value, q.scope.sensitivity.value) == (
        "dept", "internal", "sensitive",
    )
    assert q.perm_corporate is True and q.perm_individual is False
    assert q.attributes["source_refs"] == ["rec-cc-0123456789abcdef"]
    fbs = kstore.feedback_for(qid, steward)   # viewer は必須（ACL ゲートの既定値を持たせない）
    assert len(fbs) == 1 and fbs[0].body.startswith("本人確認のうえ")
    # The org owns the judgment; the approver is auditable via attributes, not authorship.
    assert fbs[0].author_id is None
    assert q.attributes["approved_by"] == "p_tanaka"


def test_sfaq_candidate_reject_creates_no_question(term_store, person_store, embedder):
    from waca_loop.embeddings import HashingEmbedder
    from waca_loop.improvement.candidates import InMemoryCandidateStore
    from waca_loop.knowledge.store import InMemoryQuestionStore
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore
    from waca_loop.models import Viewer
    from waca_loop.server import ServerContext

    kstore = InMemoryQuestionStore([], HashingEmbedder(dim=64))
    ctx = ServerContext(
        term_store=term_store, usage_store=InMemoryUsageLogStore(), embedder=embedder,
        viewer=Viewer(subject="s", person_id="p_m", is_org_member=True,
                      units=frozenset({"steward"})),
        person_store=person_store,
        candidate_store=InMemoryCandidateStore(), knowledge_store=kstore,
    )
    out = ctx.propose_candidates(
        [{"kind": "sfaq", "payload": {"question_text": "x", "judgment": "y"}}]
    )
    res = ctx.approve_candidate(out["candidate_ids"][0], approve=False)
    assert res["status"] == "rejected"
    assert res["promoted"] is False
    assert "question_id" not in res
    assert kstore._questions == []  # noqa: SLF001


def _sfaq_ctx(term_store, person_store, embedder, viewer):
    from waca_loop.embeddings import HashingEmbedder
    from waca_loop.improvement.candidates import InMemoryCandidateStore
    from waca_loop.knowledge.store import InMemoryQuestionStore
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore
    from waca_loop.server import ServerContext

    kstore = InMemoryQuestionStore([], HashingEmbedder(dim=64))
    return ServerContext(
        term_store=term_store, usage_store=InMemoryUsageLogStore(), embedder=embedder,
        viewer=viewer, person_store=person_store,
        candidate_store=InMemoryCandidateStore(), knowledge_store=kstore,
    ), kstore


def _sfaq_item(**over):
    item = {
        "kind": "sfaq",
        "payload": {"question_text": "q?", "judgment": "j"},
        "scope": {"tier": "dept", "audience": "internal", "sensitivity": "sensitive"},
        "owner_unit": "secretariat",
        "source": "cc_promote",
    }
    item.update(over)
    return item


def _steward():
    from waca_loop.models import Viewer

    # 事務局にも属するスチュワード。候補は提案者が所属するユニットにしか
    # 出せないため、secretariat の候補を出すにはその所属が要る (§3.3)。
    return Viewer(subject="s3", person_id="p_tanaka", is_org_member=True,
                  units=frozenset({"steward", "secretariat"}))


def test_sfaq_promotion_is_steward_only(term_store, person_store, embedder):
    """SPEC_phase2 §2.4 — non-steward approval of a servable SFAQ row must be refused."""
    import pytest

    from waca_loop.models import Viewer

    member = Viewer(subject="s", person_id="p_m", is_org_member=True,
                    units=frozenset({"secretariat"}))
    ctx, kstore = _sfaq_ctx(term_store, person_store, embedder, member)
    out = ctx.propose_candidates([_sfaq_item()])
    with pytest.raises(PermissionError):
        ctx.approve_candidate(out["candidate_ids"][0], approve=True)
    assert kstore._questions == []  # noqa: SLF001


def test_sfaq_promotion_is_idempotent(term_store, person_store, embedder):
    ctx, kstore = _sfaq_ctx(term_store, person_store, embedder, _steward())
    out = ctx.propose_candidates([_sfaq_item(owner_unit="steward")])
    cid = out["candidate_ids"][0]
    first = ctx.approve_candidate(cid, approve=True)
    second = ctx.approve_candidate(cid, approve=True)
    assert first["question_id"] == second["question_id"]
    assert first["promoted"] is True
    assert second["promoted"] is False, "a re-approval promotes nothing"
    assert second["already_approved"] is True
    assert len(kstore._questions) == 1  # noqa: SLF001


def test_reject_returns_no_question_id(term_store, person_store, embedder):
    ctx, _ = _sfaq_ctx(term_store, person_store, embedder, _steward())
    cid = ctx.propose_candidates([_sfaq_item()])["candidate_ids"][0]
    rejected = ctx.approve_candidate(cid, approve=False)
    assert "question_id" not in rejected
    assert rejected["status"] == "rejected"


def test_sfaq_promotion_refuses_public_audience_and_individual_provenance(
    term_store, person_store, embedder
):
    import pytest

    ctx, _ = _sfaq_ctx(term_store, person_store, embedder, _steward())
    out = ctx.propose_candidates(
        [_sfaq_item(scope={"tier": "org", "audience": "public", "sensitivity": "normal"})]
    )
    with pytest.raises(ValueError, match="audience"):
        ctx.approve_candidate(out["candidate_ids"][0], approve=True)

    out2 = ctx.propose_candidates(
        [_sfaq_item(payload={"question_text": "q?", "provenance": "individual"})]
    )
    with pytest.raises(ValueError, match="individual"):
        ctx.approve_candidate(out2["candidate_ids"][0], approve=True)


def test_approve_candidate_respects_r1_session_scope(term_store, person_store, embedder):
    """R1 — a session declared external must not write via approve_candidate either."""
    ctx, kstore = _sfaq_ctx(term_store, person_store, embedder, _steward())
    out = ctx.propose_candidates([_sfaq_item()])
    ctx.set_session_scope("external", "他組織の業務")
    res = ctx.approve_candidate(out["candidate_ids"][0], approve=True)
    assert res.get("refused") is True
    assert kstore._questions == []  # noqa: SLF001


def test_approval_is_persisted_to_the_candidate_store(
    term_store, person_store, embedder
):
    """S8 — the decision must reach the *store*, not just the fetched object.

    ``set_status`` must run on every branch, including the one where
    BigQueryCandidateStore.get() returns the candidate; otherwise the stored candidate stays
    'proposed' after a successful promotion.
    """
    from waca_loop.models import CandidateStatus

    ctx, _ = _sfaq_ctx(term_store, person_store, embedder, _steward())
    cid = ctx.propose_candidates([_sfaq_item()])["candidate_ids"][0]
    ctx.approve_candidate(cid, approve=True)
    assert ctx.candidate_store.get(cid).status == CandidateStatus.APPROVED


def test_detached_store_reapproval_does_not_mint_a_twin(
    term_store, person_store, embedder
):
    """S8 — idempotency must survive a store whose get() returns a detached copy.

    BigQueryCandidateStore rebuilds the candidate per call and never persists
    ``payload.promoted_question_id``, so only the persisted *status* can stop a second
    approval from minting a duplicate question.
    """
    import copy

    class DetachedCandidateStore:
        """Mimics the BigQuery store: get() returns a copy, payload edits are dropped."""

        def __init__(self, inner):
            self._inner = inner

        def add(self, candidate):
            return self._inner.add(candidate)

        def get(self, candidate_id):
            row = self._inner.get(candidate_id)
            if row is None:
                return None
            detached = copy.deepcopy(row)
            detached.payload.pop("promoted_question_id", None)  # not persisted in BQ
            return detached

        def set_status(self, candidate_id, status):
            self._inner.set_status(candidate_id, status)

        def list(self, *, status=None):
            return self._inner.list(status=status)

    ctx, kstore = _sfaq_ctx(term_store, person_store, embedder, _steward())
    ctx.candidate_store = DetachedCandidateStore(ctx.candidate_store)
    cid = ctx.propose_candidates([_sfaq_item()])["candidate_ids"][0]

    first = ctx.approve_candidate(cid, approve=True)
    second = ctx.approve_candidate(cid, approve=True)

    assert first["promoted"] is True
    assert second["promoted"] is False, "re-approval must not promote again"
    assert len(kstore._questions) == 1, "a twin question was minted"  # noqa: SLF001


def test_sfaq_candidate_rejection_is_steward_only(term_store, person_store, embedder):
    """SPEC_phase2 §2.4 — rejection is a steward decision too.

    The decision is now persisted, so an ungated reject would let any caller durably mark a
    steward-approved candidate 'rejected' while its minted question stays live.
    """
    import pytest

    from waca_loop.models import CandidateStatus, Viewer

    ctx, _ = _sfaq_ctx(term_store, person_store, embedder, _steward())
    cid = ctx.propose_candidates([_sfaq_item()])["candidate_ids"][0]

    ctx.viewer = Viewer(subject="s", person_id="p_m", is_org_member=True,
                        units=frozenset({"secretariat"}))
    with pytest.raises(PermissionError):
        ctx.approve_candidate(cid, approve=False)
    assert ctx.candidate_store.get(cid).status == CandidateStatus.PROPOSED


def test_reject_after_approve_is_refused(term_store, person_store, embedder):
    """S8 — a decision is terminal; the ledger must not claim a retraction that never happened.

    Rejecting an already-promoted candidate would persist 'rejected' while the minted
    question keeps being served by search_sfaq — there is no retract path here
    (set_sfaq_status on the question is the deliberate separate step).
    """
    import pytest

    from waca_loop.models import CandidateStatus

    ctx, kstore = _sfaq_ctx(term_store, person_store, embedder, _steward())
    cid = ctx.propose_candidates([_sfaq_item()])["candidate_ids"][0]
    ctx.approve_candidate(cid, approve=True)

    with pytest.raises(ValueError, match="terminal"):
        ctx.approve_candidate(cid, approve=False)
    assert ctx.candidate_store.get(cid).status == CandidateStatus.APPROVED
    assert len(kstore._questions) == 1  # noqa: SLF001


def test_approve_after_reject_is_refused(term_store, person_store, embedder):
    """S8 — a later call must not override a human's 却下 ("承認は人")."""
    import pytest

    from waca_loop.models import CandidateStatus

    ctx, kstore = _sfaq_ctx(term_store, person_store, embedder, _steward())
    cid = ctx.propose_candidates([_sfaq_item()])["candidate_ids"][0]
    ctx.approve_candidate(cid, approve=False)

    with pytest.raises(ValueError, match="terminal"):
        ctx.approve_candidate(cid, approve=True)
    assert ctx.candidate_store.get(cid).status == CandidateStatus.REJECTED
    assert kstore._questions == []  # noqa: SLF001


def test_reapproval_withholds_question_id_the_viewer_cannot_see(
    term_store, person_store, embedder
):
    """§3.3 — the reverse-link lookup is viewer-independent, so the id must not be echoed.

    set_sfaq_status is visibility-blind, so an echoed question_id is a usable write handle.
    The steward here is not in the candidate's owner_unit, so the promoted dept-scoped row
    is invisible to them and only its existence may be reported.
    """
    # 提案は所有ユニットに属する人が行い、**その外にいるスチュワード**が承認する。
    # `_steward()` は事務局にも属している（別テストの都合）ので、ここでは意図どおり
    # 「所有ユニット外」の viewer を明示的に作る — 共有の補助関数を変えると、
    # 提案を出せなくなる別テストが巻き添えになる。
    from waca_loop.models import Viewer

    ctx, kstore = _sfaq_ctx(term_store, person_store, embedder, _steward())
    cid = ctx.propose_candidates([_sfaq_item()])["candidate_ids"][0]  # dept/secretariat
    ctx.approve_candidate(cid, approve=True)

    outsider = Viewer(subject="s9", person_id="p_out", is_org_member=True,
                      units=frozenset({"steward", "dept_eng"}))
    ctx.viewer = outsider
    assert kstore.get(
        kstore.find_by_candidate_id(cid), outsider
    ) is None, "前提: この viewer には昇格先の行が見えない"

    second = ctx.approve_candidate(cid, approve=True)
    assert second["already_approved"] is True
    assert "question_id" not in second
    assert second["question_id_withheld"] is True


def test_reapproval_refuses_silently_dropping_edits(term_store, person_store, embedder):
    """S8 — a steward re-approving with corrections must not have them silently discarded."""
    import pytest

    ctx, _ = _sfaq_ctx(term_store, person_store, embedder, _steward())
    cid = ctx.propose_candidates([_sfaq_item()])["candidate_ids"][0]
    ctx.approve_candidate(cid, approve=True)
    with pytest.raises(ValueError, match="already approved"):
        ctx.approve_candidate(cid, approve=True, edits={"question_text": "amended?"})


# --- 権限ゲートの順序 ---------------------------------------
#
# 処理の順序が入れ替わると、権限ゲートが素通りする形が2つできる。どちらも
# 「新しい行は増えない」ので昇格の穴ではないが、**無権限の呼び手の名で durable な書き込み**
# をし、候補の存在と状態をその人に教える。ここで固定する。


def _member():
    from waca_loop.models import Viewer

    return Viewer(subject="m", person_id="p_m", is_org_member=True,
                  units=frozenset({"dept_eng"}))


def test_a_member_cannot_re_approve_an_already_approved_candidate(steward_context):
    """承認ゲートは冪等の早期 return より**前**にあること.

    naive な順序（早期 return が先）だと、すでに承認済みの候補について
    非スチュワードの呼び出しが `already_approved` で成功して返る。
    """
    import pytest

    # 複数ユニットに属する viewer は所有ユニットを明示しないと提案できない（§3.3）
    unit = sorted(steward_context.viewer.units)[0]
    cid = steward_context.propose_candidates(
        [{"kind": "term", "payload": {"term": "ZZ", "definition": "d"},
          "owner_unit": unit}]
    )["candidate_ids"][0]
    steward_context.approve_candidate(cid)

    steward_context.viewer = _member()
    with pytest.raises(PermissionError, match="steward"):
        steward_context.approve_candidate(cid)


def test_a_decision_is_not_written_about_a_candidate_that_cannot_be_read(steward_context):
    """読めない候補について決定を書かない（本番でだけ開くゲートの封じ）.

    本番の `BigQueryCandidateStore.get` は streaming buffer の行に正当に None を返しうる。
    「見つからないなら status だけ書く」形だと、**実在する SFAQ 候補を非スチュワードが
    却下できる** — 本番でだけゲートが開く型になる。
    """
    import pytest

    for viewer in (None, _member()):
        if viewer is not None:
            steward_context.viewer = viewer
        for approve in (True, False):
            with pytest.raises(ValueError, match="candidate not found"):
                steward_context.approve_candidate("cand_made_up", approve=approve)
