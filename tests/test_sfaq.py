"""SFAQ — 判断ナレッジ層 (SPEC_phase2). Acceptance S2–S6 + ACL double gate, offline."""

from __future__ import annotations

import pytest

from waca_loop.embeddings import HashingEmbedder
from waca_loop.grounding.terms import InMemoryTermStore
from waca_loop.knowledge.acl import question_where_clause
from waca_loop.knowledge.store import InMemoryQuestionStore, new_id
from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.models import (
    FbType,
    Feedback,
    Provenance,
    Question,
    QuestionStatus,
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Viewer,
)
from waca_loop.server import ServerContext

ORG_SCOPE = ScopeTags(ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL)


def _q(text: str, *, provenance=Provenance.CORPORATE_TRAINING, scope=ORG_SCOPE,
       owner_unit=None, perm_corporate=None, perm_individual=False,
       status=QuestionStatus.PROVISIONAL, qid=None) -> Question:
    return Question(
        question_id=qid or new_id("q"),
        question_text=text,
        scope=scope,
        provenance=provenance,
        owner_unit=owner_unit,
        perm_corporate=perm_corporate,
        perm_individual=perm_individual,
        status=status,
    )


@pytest.fixture
def member() -> Viewer:
    return Viewer(subject="s", person_id="p_m", is_org_member=True,
                  units=frozenset({"members"}))


@pytest.fixture
def kyozai() -> Viewer:
    return Viewer(subject="s2", person_id="p_k", is_org_member=True,
                  units=frozenset({"kyozai"}))


@pytest.fixture
def steward() -> Viewer:
    return Viewer(subject="s3", person_id="p_tanaka", is_org_member=True,
                  units=frozenset({"steward", "tech"}))


def _ctx(term_store, embedder, viewer, kstore) -> ServerContext:
    return ServerContext(
        term_store=term_store, usage_store=InMemoryUsageLogStore(),
        embedder=embedder, viewer=viewer, knowledge_store=kstore,
    )


def _store(*questions: Question) -> InMemoryQuestionStore:
    return InMemoryQuestionStore(list(questions), HashingEmbedder(dim=64))


def test_s2_migration_defaults_are_fail_closed(member):
    """S2: personal tier + perm_individual=false (移行既定) is invisible to everyone."""
    migrated = _q("移行データの問い", scope=ScopeTags(
        ScopeTier.PERSONAL, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL),
        provenance=Provenance.INDIVIDUAL, perm_individual=False)
    unconfirmed = _q("法人許可が未確認の問い", perm_corporate=None)  # corporate, perm unset
    store = _store(migrated, unconfirmed)

    assert store.search("問い", member, 10) == []
    assert store.get(migrated.question_id, member) is None
    assert store.get(unconfirmed.question_id, member) is None


def test_s3_corporate_permitted_individually_unpermitted_masks_author(
    term_store, embedder, member
):
    """S3 (E-3 ii): corp=true + indiv=false → served in-corp with author masked."""
    q = _q("受講データの仮名化はどう決めたか", perm_corporate=True, perm_individual=False)
    store = _store(q)
    store.add_feedback(Feedback(
        feedback_id=new_id("fb"), question_id=q.question_id,
        fb_type=FbType.DECISION, body="仮名化して安全チャネル経由と決定", author_id="p_x",
    ))

    out = _ctx(term_store, embedder, member, store).search_sfaq("仮名化", k=3)
    (hit,) = out["hits"]
    assert hit["question_id"] == q.question_id
    assert hit["feedback"][0]["author_id"] is None        # 本人特定を伏せて共有
    assert hit["feedback"][0]["body"]                     # content still served

    # ...and with individual consent the author is shown.
    q2 = _q("同意済みの問い", perm_corporate=True, perm_individual=True)
    store2 = _store(q2)
    store2.add_feedback(Feedback(
        feedback_id=new_id("fb"), question_id=q2.question_id,
        fb_type=FbType.DECISION, body="b", author_id="p_x",
    ))
    out2 = _ctx(term_store, embedder, member, store2).search_sfaq("同意済み", k=3)
    assert out2["hits"][0]["feedback"][0]["author_id"] == "p_x"


def test_s4_status_is_surfaced_and_candidate_withheld(term_store, embedder, member):
    """S4: provisional ships labeled; candidate (未整形) is never served."""
    prov = _q("暫定の問い", perm_corporate=True)
    cand = _q("候補のままの問い", perm_corporate=True, status=QuestionStatus.CANDIDATE)
    ctx = _ctx(term_store, embedder, member, _store(prov, cand))

    out = ctx.search_sfaq("問い", k=10)
    statuses = {h["question_id"]: h["status"] for h in out["hits"]}
    assert statuses.get(prov.question_id) == "provisional"
    assert cand.question_id not in statuses


def test_s5_fb_type_is_enum_validated(term_store, embedder, member):
    q = _q("フィードバック対象の問い", perm_corporate=True)
    ctx = _ctx(term_store, embedder, member, _store(q))

    ok = ctx.add_sfaq_feedback(q.question_id, "refute", "その判断は古い")
    assert ok["recorded"] is True
    with pytest.raises(ValueError):
        ctx.add_sfaq_feedback(q.question_id, "praise", "無い型")   # not one of the six


def test_s6_auto_merge_defaults_false():
    assert _q("既定確認").auto_merge is False


def test_acl_dept_scope_does_not_leak(term_store, embedder, member, kyozai):
    """Dept-tier question is visible only inside its owner_unit (§3.3 3-axis)."""
    q = _q("教材限定の問い", perm_corporate=True, owner_unit="kyozai",
           scope=ScopeTags(ScopeTier.DEPT, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL))
    store = _store(q)

    assert _ctx(term_store, embedder, kyozai, store).search_sfaq("教材限定")["hits"]
    assert not _ctx(term_store, embedder, member, store).search_sfaq("教材限定")["hits"]


def test_feedback_requires_visible_question(term_store, embedder, member):
    """No blind writes: feedback on an invisible question is rejected."""
    hidden = _q("非公開の問い", perm_corporate=False)
    ctx = _ctx(term_store, embedder, member, _store(hidden))
    with pytest.raises(ValueError):
        ctx.add_sfaq_feedback(hidden.question_id, "agree", "見えないのに書けない")


def test_add_question_files_a_candidate(term_store, embedder, member):
    ctx = _ctx(term_store, embedder, member, _store())
    out = ctx.add_sfaq_question("新しい論点", first_feedback="最初のメモ")
    assert out["status"] == "candidate"
    # candidate は配信されない（スチュワード昇格まで, §2.4）
    assert ctx.search_sfaq("新しい論点")["hits"] == []


def test_add_question_is_scoped_to_the_filers_unit(term_store, embedder, member, kyozai):
    """A: 投稿された問いは投稿者のユニット所有 dept であり、全社可視にはならない (§3.3)."""
    store = _store()
    ctx = _ctx(term_store, embedder, member, store)
    qid = ctx.add_sfaq_question("部門内の論点")["question_id"]
    store.set_status(qid, QuestionStatus.PROVISIONAL)   # candidate は配信対象外なので昇格して見る

    filed = store.get(qid, member)
    assert filed.scope.tier is ScopeTier.DEPT
    assert filed.owner_unit == "members"        # 投稿者のユニット

    # 昇格後も、別ユニットの閲覧者には見えない（org 既定なら見える）
    assert store.get(qid, kyozai) is None
    assert _ctx(term_store, embedder, kyozai, store).search_sfaq("部門内の論点")["hits"] == []


def test_add_question_fails_closed_without_an_owning_unit(term_store, embedder):
    """A(負): 所属ユニットが無い閲覧者は投稿できない — org 既定へフォールバックしない."""
    # org は全員の unit_path に入るので「所有ユニット」にならない (ORG_WIDE_UNITS)
    rootless = Viewer(subject="s9", person_id="p_x", is_org_member=True,
                      units=frozenset({"org"}))
    with pytest.raises(PermissionError):
        _ctx(term_store, embedder, rootless, _store()).add_sfaq_question("宛先のない問い")


def test_add_question_refuses_to_guess_for_multi_unit_filers(term_store, embedder):
    """A(負): 複数ユニット所属者にはユニットを言わせる — ハッシュ順/辞書順で勝手に決めない."""
    multi = Viewer(subject="s10", person_id="p_yamada", is_org_member=True,
                   units=frozenset({"secretariat", "kyozai", "org"}))
    store = _store()
    ctx = _ctx(term_store, embedder, multi, store)

    with pytest.raises(PermissionError, match="kyozai"):   # 選択肢を示して拒否
        ctx.add_sfaq_question("どちらの部門の問いか不明")

    # 所属していないユニットへの指定も拒否（他部門への投げ込み防止）
    with pytest.raises(PermissionError):
        ctx.add_sfaq_question("他部門の問い", owner_unit="tech")

    qid = ctx.add_sfaq_question("事務局の問い", owner_unit="secretariat")["question_id"]
    store.set_status(qid, QuestionStatus.PROVISIONAL)
    assert store.get(qid, multi).owner_unit == "secretariat"


def test_status_transition_is_steward_only(term_store, embedder, member, steward):
    q = _q("承認待ちの問い", perm_corporate=True)
    store = _store(q)

    with pytest.raises(PermissionError):
        _ctx(term_store, embedder, member, store).set_sfaq_status(q.question_id, "approved")

    out = _ctx(term_store, embedder, steward, store).set_sfaq_status(q.question_id, "approved")
    assert out["status"] == "approved"
    assert store.get(q.question_id, steward).status is QuestionStatus.APPROVED


def test_where_clause_mirrors_perm_gate(member):
    """Gate 1 SQL twin carries the E-3 CASE and the servable-status filter."""
    sql, params = question_where_clause(member, alias="q")
    assert "CASE q.provenance" in sql and "perm_corporate" in sql
    assert "q.status IN ('provisional', 'approved', 'contested')" in sql
    assert params  # scope params present (bound by the caller)


# --- 公開範囲の変更 (§2.4/§3.3) — 全社公開と対になるガード ---

DEPT_SCOPE = ScopeTags(ScopeTier.DEPT, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL)
SENSITIVE_DEPT = ScopeTags(ScopeTier.DEPT, ScopeAudience.INTERNAL, ScopeSensitivity.SENSITIVE)


def test_a_steward_can_widen_a_filed_question_to_the_org(term_store, embedder, steward):
    """add_sfaq_question は必ず dept で入る。広げる道が無いと再登録するしかない."""
    q = _q("部門で決めたが全社に関わる判断", scope=DEPT_SCOPE, owner_unit="steward",
           provenance=Provenance.INDIVIDUAL, perm_individual=True)
    store = _store(q)
    out = _ctx(term_store, embedder, steward, store).set_sfaq_scope(q.question_id, "org")
    assert out["scope_tier"] == "org"
    assert store.get(q.question_id, steward).scope.tier is ScopeTier.ORG


def test_a_board_owned_judgment_can_never_go_org(term_store, embedder, steward):
    """理事会所有の行は全社公開にできない (§3.3).

    既存の知識をまとめて全社公開にするのは、用語やカリキュラムには正しい判断でも、
    理事会の議事には当てはまらない。**開くことが易しい道である**以上、閉じるほうは
    記憶ではなく構造で担保する必要がある。
    """
    from waca_loop.retrieval.acl import BOARD_UNIT

    q = _q("人事に関わる理事会決定", scope=DEPT_SCOPE, owner_unit=BOARD_UNIT,
           provenance=Provenance.INDIVIDUAL, perm_individual=True)
    board_steward = Viewer(subject="s9", person_id="p_b", is_org_member=True,
                           units=frozenset({"steward", BOARD_UNIT}))
    ctx = _ctx(term_store, embedder, board_steward, _store(q))
    with pytest.raises(PermissionError, match="理事会"):
        ctx.set_sfaq_scope(q.question_id, "org")


def test_a_sensitive_row_cannot_be_widened_while_still_sensitive(term_store, embedder):
    """tier だけ広げて sensitivity を残すと、2つの軸が食い違う行ができる (§3.3)."""
    q = _q("機微な判断", scope=SENSITIVE_DEPT, owner_unit="steward",
           provenance=Provenance.INDIVIDUAL, perm_individual=True)
    steward = Viewer(subject="s3", person_id="p_tanaka", is_org_member=True,
                     units=frozenset({"steward"}))
    ctx = _ctx(term_store, embedder, steward, _store(q))
    with pytest.raises(PermissionError, match="機微"):
        ctx.set_sfaq_scope(q.question_id, "org")

    # 同じ呼び出しで機微を外しながら公開することもできない。**二度に分ける**必要がある —
    # そうしないと「機微を解除した」判断が公開の陰に隠れ、痕跡が1本にまとまってしまう。
    with pytest.raises(PermissionError, match="機微"):
        ctx.set_sfaq_scope(q.question_id, "org", sensitivity="normal")

    ctx.set_sfaq_scope(q.question_id, "dept", sensitivity="normal")   # ① 解除
    out = ctx.set_sfaq_scope(q.question_id, "org")                     # ② 公開
    assert out["scope_tier"] == "org" and out["scope_sensitivity"] == "normal"


def test_only_a_steward_may_rescope(term_store, embedder, kyozai):
    q = _q("教材の判断", scope=DEPT_SCOPE, owner_unit="kyozai",
           provenance=Provenance.INDIVIDUAL, perm_individual=True)
    ctx = _ctx(term_store, embedder, kyozai, _store(q))
    with pytest.raises(PermissionError, match="steward"):
        ctx.set_sfaq_scope(q.question_id, "org")


def test_a_steward_cannot_rescope_what_they_cannot_see(term_store, embedder):
    """見えない行は書き換えられない — 盲目の書き込みを作らない (§3.3)."""
    q = _q("別部門の機微", scope=SENSITIVE_DEPT, owner_unit="kyozai",
           provenance=Provenance.INDIVIDUAL, perm_individual=True)
    steward = Viewer(subject="s3", person_id="p_tanaka", is_org_member=True,
                     units=frozenset({"steward"}))   # kyozai を持たない
    ctx = _ctx(term_store, embedder, steward, _store(q))
    with pytest.raises(ValueError, match="not found or not visible"):
        ctx.set_sfaq_scope(q.question_id, "dept")


# --- feedback の読み口に既定値を持たせない ---


def test_feedback_for_requires_a_viewer():
    """ACL ゲートに既定値を持たせない — 呼び忘れた経路で黙って無効になるため."""
    import inspect

    from waca_loop.knowledge.store import BigQueryQuestionStore, InMemoryQuestionStore

    for cls in (InMemoryQuestionStore, BigQueryQuestionStore):
        for name in ("feedback_for", "feedback_for_many"):
            sig = inspect.signature(getattr(cls, name))
            assert sig.parameters["viewer"].default is inspect.Parameter.empty, (
                f"{cls.__name__}.{name} の viewer に既定値がある"
            )


def test_feedback_of_an_invisible_question_is_not_returned(member, kyozai):
    """見えない問いのフィードバックは、question_id を知っていても返らない."""
    from waca_loop.models import Feedback

    dept = _q("部門限りの判断", scope=ScopeTags(
        ScopeTier.DEPT, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL),
        owner_unit="kyozai", perm_corporate=True, qid="q_dept")
    store = _store(dept)
    store.add_feedback(Feedback(feedback_id="f1", question_id="q_dept",
                                fb_type=FbType.DECISION, body="部門の判断本文"))

    assert store.feedback_for("q_dept", kyozai), "所有部門から見えていない"
    assert store.feedback_for("q_dept", member) == [], "他部門に本文が返っている"
    assert store.feedback_for_many(["q_dept"], member) == {}


def test_question_and_feedback_timestamps_round_trip_through_rows():
    """SPEC_phase3 §2.2: ERN freshness depends on preserving SFAQ timestamps in both twins."""
    from datetime import UTC, datetime

    from waca_loop.knowledge.store import (
        _row_to_feedback,
        _row_to_question,
        feedback_to_row,
        question_to_row,
    )

    created = datetime(2026, 8, 27, 1, 2, 3, tzinfo=UTC)
    updated = datetime(2026, 8, 27, 4, 5, 6, tzinfo=UTC)
    q = Question(
        question_id="q_ts",
        question_text="鮮度減衰の問い",
        scope=ORG_SCOPE,
        provenance=Provenance.OTHER,
        created_at=created,
        updated_at=updated,
    )
    q2 = _row_to_question(question_to_row(q))
    assert q2.created_at == created
    assert q2.updated_at == updated

    fb = Feedback(
        feedback_id="fb_ts",
        question_id="q_ts",
        fb_type=FbType.DECISION,
        body="判断本文",
        created_at=created,
    )
    fb2 = _row_to_feedback(feedback_to_row(fb))
    assert fb2.created_at == created


def test_search_looks_at_the_body_not_only_the_heading():
    """字面・ベクトルの両腕が `embed_text` を見ること.

    見出しに無く本文にしかない語で引いて当たる、が最小の証明。ここが通らないと
    「揺らぎを吸収する場所を正規表現からベクトルへ移す」が成立していない。
    """
    from waca_loop.embeddings import HashingEmbedder
    from waca_loop.knowledge.store import InMemoryQuestionStore
    from waca_loop.models import (
        Provenance,
        Question,
        QuestionStatus,
        ScopeAudience,
        ScopeSensitivity,
        ScopeTags,
        ScopeTier,
        Viewer,
    )

    def _q(qid: str, heading: str, embed_text: str | None) -> Question:
        return Question(
            question_id=qid,
            question_text=heading,
            embed_text=embed_text,
            scope=ScopeTags(ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL),
            provenance=Provenance.CORPORATE_TRAINING,
            perm_corporate=True,
            status=QuestionStatus.APPROVED,
        )

    target = _q(
        "q_body", "これは断定である。 — このとき、どう判断するか？",
        "これは断定である。 — このとき、どう判断するか？\n"
        "週次のロードマップ棚卸しで、優先度の入れ替えを決めているのである。",
    )
    other = _q("q_other", "まったく別の見出しである。 — このとき、どう判断するか？", None)
    store = InMemoryQuestionStore([target, other], embedder=HashingEmbedder(dim=64))
    viewer = Viewer(subject="s", person_id="p1", is_org_member=True, units=frozenset())

    # 「ロードマップ棚卸し」は見出しに無く、本文にしかない
    assert "ロードマップ棚卸し" not in target.question_text
    hits = store.search("ロードマップ棚卸し", viewer, limit=2)
    assert hits and hits[0].question_id == "q_body"


def test_embed_text_absent_falls_back_to_the_heading():
    """`embed_text` を持たない行がそのまま動くこと（見出しへ戻る）."""
    from waca_loop.models import (
        Provenance,
        Question,
        ScopeAudience,
        ScopeSensitivity,
        ScopeTags,
        ScopeTier,
    )

    q = Question(
        question_id="q1", question_text="見出しである。",
        scope=ScopeTags(ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL),
        provenance=Provenance.OTHER,
    )
    assert q.embed_text is None
    assert q.search_text() == "見出しである。"


def _task_ctx(evidence: str, owner_unit: str = "kyozai"):
    """steward 視点の ServerContext と、1件だけタスクを持つストア."""
    task = {
        "task_id": "t1", "title": "申告", "evidence": evidence,
        "owner_unit": owner_unit, "status": "open", "kind": "incident",
    }

    class _Tasks:
        def list(self, status=None, kind=None, viewer=None):
            return [dict(task)]

    class _Persons:
        def roster_names(self):
            return ()

    viewer = Viewer(
        subject="s", person_id="p_steward", is_org_member=True,
        units=frozenset({"steward", "kyozai", "board"}),
    )
    return ServerContext(
        term_store=InMemoryTermStore([], HashingEmbedder(dim=64)),
        usage_store=InMemoryUsageLogStore(),
        embedder=HashingEmbedder(dim=64), viewer=viewer,
        knowledge_store=InMemoryQuestionStore([]), task_store=_Tasks(),
        person_store=_Persons(),
    )


def test_first_feedback_cannot_smuggle_the_report_body_org_wide():
    """申告本文のコピー検査が `first_feedback` にも掛かること.

    このツールは docstring で「evidence を写さない」と約束している。検査が first_feedback に
    掛からなければ、2番目の引数から逐語がそのまま入り、org 固定の問いにぶら下がって
    全社へ配られる。
    """
    import pytest

    evidence = "教材部の部門長が会員名簿をChatGPTに貼った。氏名とメールが含まれていた。"
    ctx = _task_ctx(evidence)
    with pytest.raises(ValueError, match="フィードバック"):
        ctx.raise_lesson_from_task(
            "t1",
            "名簿の持ち出しは仕組みで防ぐべきである",
            first_feedback=evidence,
        )


def test_the_copy_check_is_not_defeated_by_a_space_or_one_character():
    """空白・改行・一文字の削除で検査を抜けられないこと.

    生の部分一致は「急いで少しだけ触った形」を通す。それを通すなら検査が無いのと同じ。
    """
    import pytest

    evidence = "教材部の部門長が会員名簿をChatGPTに貼った。氏名とメールが含まれていた。"
    for tampered in (
        evidence.replace("。", "。 "),                    # 句点のあとに空白
        evidence.replace("貼った", "貼った\n"),            # 改行を挟む
        evidence.replace("氏名と", "氏名"),                # 一文字消す
    ):
        ctx = _task_ctx(evidence)
        with pytest.raises(ValueError):
            ctx.raise_lesson_from_task("t1", tampered)


def test_a_board_owned_task_cannot_raise_an_org_wide_lesson():
    """作成経路にも理事会ガードが掛かること.

    `set_sfaq_scope` は理事会所有の判断を org へ広げることを拒否する。この経路が起点の
    ACL を見ずに ORG 固定で作ると、同じ内容が片方で拒否され片方で通ってしまう。
    """
    import pytest

    ctx = _task_ctx("申告の本文である", owner_unit="board")
    with pytest.raises(PermissionError, match="理事会"):
        ctx.raise_lesson_from_task("t1", "一般化した学びである")


def test_a_normal_lesson_still_gets_raised():
    """ガードを足しても通常の経路が通ること（締めすぎていないことの確認）."""
    ctx = _task_ctx("教材部の部門長が会員名簿をChatGPTに貼った。")
    out = ctx.raise_lesson_from_task(
        "t1", "名簿の持ち出しは、運用の注意ではなく仕組みで防ぐべきである",
        first_feedback="次の理事会で仕組み側の対策を諮る",
    )
    assert out["status"] == "provisional"
    assert out["source_task_id"] == "t1"
