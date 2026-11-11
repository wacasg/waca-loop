"""S9 — list_improvement_tasks の viewer-ACL: incident は所属ユニットのみ.

Improvement tasks (incident-derived ones are
sensitive) must be ACL-filtered by viewer, enforced twice (in-process + SQL WHERE).
"""

from __future__ import annotations

import pytest

from waca_loop.improvement.acl import task_acl_where_clause, task_visible_to
from waca_loop.improvement.tasks import InMemoryTaskStore
from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.models import Viewer
from waca_loop.server import ServerContext

_INCIDENT = {"kind": "incident", "owner_unit": "dept_sales"}
_HARMFUL = {"kind": "harmful", "owner_unit": None}


def test_visible_to_rules(viewer_member, viewer_sales, viewer_outsider):
    # Non-incident: any org member sees it; outsider does not.
    assert task_visible_to(_HARMFUL, viewer_member)
    assert not task_visible_to(_HARMFUL, viewer_outsider)
    # Incident (dept_sales): only the owning unit; other units and outsiders blocked.
    assert task_visible_to(_INCIDENT, viewer_sales)
    assert not task_visible_to(_INCIDENT, viewer_member)   # dept_eng != dept_sales
    assert not task_visible_to(_INCIDENT, viewer_outsider)


def test_incident_without_owner_unit_is_fail_closed(viewer_sales):
    assert not task_visible_to({"kind": "incident", "owner_unit": None}, viewer_sales)


def test_owned_non_incident_task_is_unit_restricted(viewer_member, viewer_sales):
    """incident 由来の harmful/nps も所有ユニットで絞る(種別だけで判定しない)."""
    owned = {"kind": "harmful", "owner_unit": "dept_eng"}
    assert task_visible_to(owned, viewer_member)
    assert not task_visible_to(owned, viewer_sales)
    # 所有ユニットの無い通常のフィードバックは組織内共有
    assert task_visible_to({"kind": "harmful", "owner_unit": None}, viewer_sales)
    # '' は NULL と同じ「所有なし」— インプロセスと SQL(NULLIF) で判定が割れないこと
    assert task_visible_to({"kind": "harmful", "owner_unit": ""}, viewer_sales)


def test_where_clause_mirrors_rules(viewer_member):
    sql, params = task_acl_where_clause(viewer_member)
    assert "kind != 'incident'" in sql
    # 種別だけでなく所有の有無も見る。'' は NULL と同じ「所有なし」に正規化する
    assert "NULLIF(owner_unit, '') IS NULL" in sql
    assert "owner_unit" in sql and "IN UNNEST(@task_units)" in sql
    assert params["task_is_org_member"] is True
    assert params["task_units"] == ["dept_eng"]


def _ctx(viewer: Viewer, task_store: InMemoryTaskStore, term_store, person_store, embedder):
    from waca_loop.improvement.candidates import InMemoryCandidateStore
    from waca_loop.survey.store import InMemorySurveyStore

    return ServerContext(
        term_store=term_store,
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=viewer,
        person_store=person_store,
        survey_store=InMemorySurveyStore(),
        task_store=task_store,
        candidate_store=InMemoryCandidateStore(),
    )


def test_list_filters_incident_across_viewers(term_store, person_store, embedder,
                                              viewer_member, viewer_sales):
    """An incident raised in dept_eng is visible to a dept_eng viewer but not a dept_sales one."""
    shared_tasks = InMemoryTaskStore()

    # viewer_member is in dept_eng -> incident owner_unit becomes dept_eng.
    eng_ctx = _ctx(viewer_member, shared_tasks, term_store, person_store, embedder)
    eng_ctx.submit_survey_response(
        nps_score=2, nps_reason="不安", useful="", harmful="サンプル協会の説明が誤り",
        incident_flag=True, incident_summary="名簿混入",
    )

    # dept_eng viewer sees the incident task; dept_sales viewer does not.
    eng_kinds = {t["kind"] for t in eng_ctx.list_improvement_tasks()["tasks"]}
    assert "incident" in eng_kinds

    sales_ctx = _ctx(viewer_sales, shared_tasks, term_store, person_store, embedder)
    sales_kinds = {t["kind"] for t in sales_ctx.list_improvement_tasks()["tasks"]}
    assert "incident" not in sales_kinds          # incident hidden from other unit
    # incident 申告から派生した harmful / nps_detractor も
    # 同じ本文を evidence に持つ（incident の evidence は harmful_summary にフォールバック
    # さえする）。「非 incident だから全社可視」にすると、申告が種別違いの課題として
    # 組織全体に出る。同じ提出から出た課題は同じ所有ユニットに属する。
    assert sales_kinds == set()

    # incident でない普通のフィードバックも、提出時に回答者へ返した
    # 届け先(owner_unit)にだけ見える。全社共有にすると、「あなたの部門が見ます」と
    # 伝えながら本文が組織全体に出る。学びの全社共有は失うが、
    # 届け先の約束が守られないほうが悪い壊れ方である。
    eng_ctx.submit_survey_response(
        nps_score=2, nps_reason="使いにくい", useful="", harmful="用語の説明が古い",
        incident_flag=False,
    )
    assert {t["kind"] for t in sales_ctx.list_improvement_tasks()["tasks"]} == set()
    assert "harmful" in {t["kind"] for t in eng_ctx.list_improvement_tasks()["tasks"]}


def test_outsider_sees_no_tasks(term_store, person_store, embedder, viewer_member):
    shared_tasks = InMemoryTaskStore()
    eng_ctx = _ctx(viewer_member, shared_tasks, term_store, person_store, embedder)
    eng_ctx.submit_survey_response(
        nps_score=3, nps_reason="x", useful="", harmful="誤り", incident_flag=False,
    )
    outsider = Viewer.anonymous(subject="sub_hashed_out")
    out_ctx = _ctx(outsider, shared_tasks, term_store, person_store, embedder)
    assert out_ctx.list_improvement_tasks()["tasks"] == []


def test_the_promised_audience_is_the_actual_audience(term_store, person_store, embedder,
                                                      viewer_member, viewer_sales):
    """提出時に返した届け先が、実際の届け先であること.

    `submit_survey_response` は返り値の `owner_unit` を「誰が見るか」として回答者に伝える
    よう指示している。機微でない申告でもそれが守られなければならない。「あなたの部門が
    見ます」と伝えながら harmful_summary が組織全員に — 申告の対象になっている当人を含めて —
    逐語で出てはならない。約束した相手以外に見えてはならない。
    """
    shared_tasks = InMemoryTaskStore()
    eng_ctx = _ctx(viewer_member, shared_tasks, term_store, person_store, embedder)
    out = eng_ctx.submit_survey_response(
        nps_score=1, nps_reason="不満", useful="",
        harmful="部門長がAIに会員名簿を貼り付けていた", incident_flag=False,
    )
    promised = out["owner_unit"]
    assert promised                                   # 届け先を名指ししている

    # 派生タスクは1件残らず、約束した届け先が持ち主であること
    rows = shared_tasks.list(status=None, kind=None, viewer=viewer_member)
    assert rows, "no task was derived"
    assert {r["owner_unit"] for r in rows} == {promised}

    # 約束の外にいる人には、種別を問わず1件も見えないこと
    sales_ctx = _ctx(viewer_sales, shared_tasks, term_store, person_store, embedder)
    assert sales_ctx.list_improvement_tasks()["tasks"] == []


def test_listing_tasks_without_a_viewer_is_not_possible():
    """ACL を外す既定値を残さない (§3.3).

    `viewer=None` 既定は「ゲート無し」を意味し、しかも SQL WHERE と in-process の
    **二重ゲートが同時に落ちる**。一人のうっかりで防御が丸ごと消える形は、安全な道が
    易しい道になっていない。必須引数にして、そもそも書けなくする。
    """
    with pytest.raises(TypeError):
        InMemoryTaskStore().list(status=None, kind=None)


def test_a_steward_can_see_the_whole_backlog(term_store, person_store, embedder,
                                             viewer_member, viewer_sales):
    """横断視界: スチュワードには他部門の課題も見えること.

    全タスクに所有ユニットを刻むと、改善バックログ全体を見渡せる人が誰もいなくなる。
    俯瞰はガバナンス役の仕事そのものであり、特定の役割であって「聴衆」ではない。所有の
    規則を全社可視へ緩めるのではなく、スチュワードにだけ狭く開ける。
    """
    from waca_loop.models import Viewer

    shared = InMemoryTaskStore()
    _ctx(viewer_member, shared, term_store, person_store, embedder).submit_survey_response(
        nps_score=2, nps_reason="", useful="", harmful="用語の説明が古い", incident_flag=False,
    )
    _ctx(viewer_sales, shared, term_store, person_store, embedder).submit_survey_response(
        nps_score=1, nps_reason="", useful="", harmful="別部門の失敗", incident_flag=False,
    )

    steward = Viewer(subject="sub_s", person_id="p_s", units=frozenset({"steward"}),
                     is_org_member=True)
    seen = _ctx(steward, shared, term_store, person_store, embedder).list_improvement_tasks()
    assert len(seen["tasks"]) >= 2
    assert {"用語の説明が古い", "別部門の失敗"} <= {t["evidence"] for t in seen["tasks"]}

    # それでも一般メンバーは他部門のものを見られない(緩めたのはスチュワードだけ)
    other = _ctx(viewer_sales, shared, term_store, person_store, embedder)
    seen_by_other = {t["evidence"] for t in other.list_improvement_tasks()["tasks"]}
    assert "用語の説明が古い" not in seen_by_other


def test_raising_a_lesson_does_not_copy_the_report(term_store, person_store, embedder,
                                                   viewer_member):
    """(b) 学び起こし: 申告の本文は写さない。人が書いた一般化だけを載せる.

    改善タスクは届け先の部門にだけ見える。約束を守る代わりに、他部門の失敗が部門をまたいで流れる道が消える。矛盾するのは一点だけで、
    **いま「学び」と「申告」が同じ1本の文字列だから**である。そのまま広げれば、
    学びと一緒に誰が誰を告発したかも広がる。だから写さない。
    """
    from waca_loop.knowledge.store import InMemoryQuestionStore
    from waca_loop.models import Viewer

    shared = InMemoryTaskStore()
    _ctx(viewer_member, shared, term_store, person_store, embedder).submit_survey_response(
        nps_score=1, nps_reason="", useful="",
        harmful="部門長がAIに会員名簿を貼り付けていた", incident_flag=False,
    )
    steward = Viewer(subject="s", person_id="p_s", is_org_member=True,
                     units=frozenset({"steward"}))
    kstore = InMemoryQuestionStore([], embedder)
    ctx = _ctx(steward, shared, term_store, person_store, embedder)
    ctx.knowledge_store = kstore

    task = ctx.list_improvement_tasks()["tasks"][0]

    # 申告の本文をそのまま貼ると拒否される
    with pytest.raises(ValueError, match="書き直して"):
        ctx.raise_lesson_from_task(task["task_id"], task["evidence"])

    out = ctx.raise_lesson_from_task(
        task["task_id"], "会員名簿をAIに貼る前に、匿名化の要否をどう判断するか？")
    q = kstore.get(out["question_id"], steward)
    assert "部門長" not in q.question_text          # 誰の話かが消えている
    assert q.attributes["source_task_id"] == task["task_id"]   # 辿れる道は残る
    assert q.status.value == "provisional"          # 起こしただけでは確定ではない


def test_only_a_steward_may_raise_a_lesson(term_store, person_store, embedder, viewer_member):
    from waca_loop.knowledge.store import InMemoryQuestionStore

    shared = InMemoryTaskStore()
    ctx = _ctx(viewer_member, shared, term_store, person_store, embedder)
    ctx.knowledge_store = InMemoryQuestionStore([], embedder)
    ctx.submit_survey_response(nps_score=1, nps_reason="", useful="",
                               harmful="何か", incident_flag=False)
    task = ctx.list_improvement_tasks()["tasks"][0]
    with pytest.raises(PermissionError, match="steward"):
        ctx.raise_lesson_from_task(task["task_id"], "一般化した学び")


# --- 写し検査の抜け道 ----
#
# 「申告の本文は写さない」の検査が `evidence in lesson_text` の1行だけだと抜け道が3つある。
# ここで塞いだ3つを固定する。

def _steward_ctx(term_store, person_store, embedder, viewer_member, harmful: str,
                 incident_flag: bool = False):
    from waca_loop.knowledge.store import InMemoryQuestionStore
    from waca_loop.models import Viewer

    shared = InMemoryTaskStore()
    _ctx(viewer_member, shared, term_store, person_store, embedder).submit_survey_response(
        nps_score=1, nps_reason="対応が遅い", useful="", harmful=harmful,
        incident_flag=incident_flag,
    )
    steward = Viewer(subject="s", person_id="p_s", is_org_member=True,
                     units=frozenset({"steward"}))
    ctx = _ctx(steward, shared, term_store, person_store, embedder)
    ctx.knowledge_store = InMemoryQuestionStore([], embedder)
    return ctx


def test_evidence_of_a_sibling_task_cannot_be_pasted(term_store, person_store, embedder,
                                                     viewer_member):
    """ひとつの申告から複数タスクが派生する。A を指しながら B の本文を貼れてはならない.

    **申告本文に氏名も連絡先も置かないこと**が、このテストが噛むための条件である。
    evidence が名簿の氏名（conftest の 佐藤 など）を含むと氏名ゲートが先に閉じ、
    「兄弟タスクの evidence も突き合わせる」検査を外しても緑のままになる。拒否された門を
    メッセージで名指しし、他の門に肩代わりさせない。
    """
    ctx = _steward_ctx(term_store, person_store, embedder, viewer_member,
                       harmful="部門の共有ドライブにある会員名簿をそのまま生成AIへ投入し、"
                               "集計結果を社外の資料に転記していた",
                       incident_flag=True)
    tasks = ctx.list_improvement_tasks()["tasks"]
    assert len(tasks) >= 2, "同じ申告から派生した兄弟タスクが無い（前提が崩れている）"

    other = next(t for t in tasks if t["task_id"] != tasks[0]["task_id"] and t["evidence"])
    assert other["evidence"] != tasks[0]["evidence"], "兄弟の本文が同じでは検査にならない"
    with pytest.raises(ValueError, match="申告の本文が写されている"):
        ctx.raise_lesson_from_task(tasks[0]["task_id"], other["evidence"])


def test_the_lesson_is_not_filed_when_the_roster_cannot_be_read(term_store, embedder,
                                                               viewer_member):
    """名簿を引けない構築経路では、素通りさせずに**断る**.

    起票される Question は ORG 可視である。氏名ゲートを欠いたまま書くくらいなら書かない。
    naive 版（`person_store` が無ければ氏名検査を飛ばす）だと、他のテストは全部緑のまま
    ここだけがすり抜ける — person_store を持たない構築経路がありうる。
    """
    from waca_loop.knowledge.store import InMemoryQuestionStore
    from waca_loop.models import Viewer

    shared = InMemoryTaskStore()
    _ctx(viewer_member, shared, term_store, None, embedder).submit_survey_response(
        nps_score=1, nps_reason="", useful="",
        harmful="申請の手順が周知されていなかった", incident_flag=False,
    )
    steward = Viewer(subject="s", person_id="p_s", is_org_member=True,
                     units=frozenset({"steward"}))
    ctx = _ctx(steward, shared, term_store, None, embedder)
    ctx.knowledge_store = InMemoryQuestionStore([], embedder)
    task = ctx.list_improvement_tasks()["tasks"][0]

    with pytest.raises(RuntimeError, match="氏名の検査"):
        ctx.raise_lesson_from_task(task["task_id"], "申請の手順をどう周知するか？")


def test_a_partial_paste_is_refused(term_store, person_store, embedder, viewer_member):
    """**中ほどを切り出した**部分貼りも写しである（丸写しではないので窓判定が要る）.

    evidence の先頭を貼るだけだと、evidence が短い場合に「全文をそのまま貼る」テストに
    しかならず、完全一致の包含だけの実装でも通ってしまう。中ほどを切り出して、窓判定の
    経路を検査する。
    """
    ctx = _steward_ctx(
        term_store, person_store, embedder, viewer_member,
        harmful="営業部の担当者が顧客の連絡先一覧をそのまま外部の生成AIに投入し、"
                "個人情報を含むファイルを社外へ持ち出す形になっていた",
    )
    task = ctx.list_improvement_tasks()["tasks"][0]
    middle = task["evidence"][10:44]
    assert middle not in task["evidence"][:10], "切り出しが先頭からになっている"
    with pytest.raises(ValueError, match="書き直して"):
        ctx.raise_lesson_from_task(
            task["task_id"], f"今回の件を踏まえて考える。{middle}。これをどう防ぐか？")


def test_a_legitimate_generalization_is_not_blocked(term_store, person_store, embedder,
                                                    viewer_member):
    """塞ぎすぎの検査。**同じ業務語彙**を使った正当な一般化は通ること。

    通らなければ誰も学びを起こさなくなり、それは漏れるのとは別の形の失敗である。
    """
    ctx = _steward_ctx(term_store, person_store, embedder, viewer_member,
                       harmful="対応が遅い")
    task = next(t for t in ctx.list_improvement_tasks()["tasks"] if t["evidence"])
    out = ctx.raise_lesson_from_task(
        task["task_id"], "対応が遅いと感じさせないために、初動でいつまでに返すかを伝える")
    assert out["question_id"]


def test_a_staff_members_name_is_refused(term_store, person_store, embedder, viewer_member):
    """氏名は正規表現では捕まらない。名簿で見る."""
    ctx = _steward_ctx(term_store, person_store, embedder, viewer_member,
                       harmful="担当者が資料の取り扱いを誤っていた")
    task = ctx.list_improvement_tasks()["tasks"][0]
    roster = ctx.person_store.roster_names()
    assert roster, "名簿が空でこのテストは意味を持たない"
    with pytest.raises(ValueError, match="氏名"):
        ctx.raise_lesson_from_task(
            task["task_id"], f"{roster[0]}の件を踏まえ、資料の取り扱いを見直す")


def test_full_width_contact_details_are_refused(term_store, person_store, embedder,
                                                viewer_member):
    """日本語入力の既定は全角。畳まないと素通りする."""
    ctx = _steward_ctx(term_store, person_store, embedder, viewer_member,
                       harmful="担当者が資料の取り扱いを誤っていた")
    task = ctx.list_improvement_tasks()["tasks"][0]
    with pytest.raises(ValueError, match="個人を特定できる情報"):
        ctx.raise_lesson_from_task(
            task["task_id"], "連絡先（ｓａｔｏ＠ｅｘａｍｐｌｅ．ｃｏｍ）を本文に書かない")


def test_first_feedback_is_checked_too(term_store, person_store, embedder, viewer_member):
    """見出しを一般化しても、first_feedback に写せば同じこと。

    本文は埋め込みの主対象なので、ここが素通りだと
    「一般化した見出し＋生の申告本文」が全社の意味検索に載る。
    """
    ctx = _steward_ctx(term_store, person_store, embedder, viewer_member,
                       harmful="経理の担当者が請求書PDFを外部AIにアップロードしていた")
    task = ctx.list_improvement_tasks()["tasks"][0]
    with pytest.raises(ValueError, match="書き直して"):
        ctx.raise_lesson_from_task(
            task["task_id"], "社外AIへの資料アップロードをどう判断するか？",
            first_feedback=task["evidence"],
        )


def test_personal_data_left_in_the_lesson_is_refused(term_store, person_store, embedder,
                                                     viewer_member):
    """学びは全社に置かれる。連絡先が残っていたら通さない（FLOOR-01 と同じ向き）."""
    ctx = _steward_ctx(term_store, person_store, embedder, viewer_member,
                       harmful="担当者が個人情報を含む資料を扱っていた")
    task = ctx.list_improvement_tasks()["tasks"][0]
    with pytest.raises(ValueError, match="個人を特定できる情報"):
        ctx.raise_lesson_from_task(
            task["task_id"],
            "連絡先（sato@example.net）を含む資料をAIに渡す前に何を確認するか？",
        )


def test_the_second_step_cannot_smuggle_the_report_back_in(term_store, person_store,
                                                           embedder, viewer_member):
    """(b') 学びを起こしてから申告本文を積み直す2手を塞ぐ.

    ガードが `raise_lesson_from_task` にしか無いと、「きれいな学びを起こす →
    `add_sfaq_feedback` で生の本文を足す」の2手で全社公開に到達できる。
    `search_sfaq` は feedback の本文を org メンバーへそのまま返すので、
    入口を1つ塞いでも裏口が開いていれば保護は残らない。
    """
    ctx = _steward_ctx(term_store, person_store, embedder, viewer_member,
                       harmful="部門長が会員名簿をそのまま生成AIへ投入していた")
    task = ctx.list_improvement_tasks()["tasks"][0]

    out = ctx.raise_lesson_from_task(
        task["task_id"], "会員名簿をAIに貼る前に、匿名化の要否をどう判断するか？")

    # 1手目は通ったが、2手目で申告本文を積もうとすると同じ検査に当たる
    with pytest.raises(ValueError, match="書き直して"):
        ctx.add_sfaq_feedback(out["question_id"], "decision", task["evidence"])

    # 一般化された判断は積める — 塞いだせいで道具が使えなくなっていないこと
    ok = ctx.add_sfaq_feedback(
        out["question_id"], "decision", "列名だけで匿名化仕様を確認してから渡す運用にした")
    assert ok["recorded"] is True


# --- 裏口も表口と同じ検査を通ること-----------------
#
# 本番の検査経路は lesson_guard に一本化してある。裏口（add_sfaq_feedback）にも同じ
# 検査が効いていることを、ここで固定する。


def _lesson(ctx):
    """タスクから学びを起こして (question_id, 元タスク, 兄弟タスク) を返す。"""
    tasks = ctx.list_improvement_tasks()["tasks"]
    t0 = tasks[0]
    sibling = next(
        (t for t in tasks if t["task_id"] != t0["task_id"] and t["evidence"]), None
    )
    out = ctx.raise_lesson_from_task(t0["task_id"], "外部AIへ資料を渡す前に何を確認するか？")
    return out["question_id"], t0, sibling


def test_the_back_door_refuses_a_punctuation_split_paste(term_store, person_store, embedder,
                                                         viewer_member):
    """句読点を挟んだ部分貼り。句読点を落とさない実装では素通りしてしまう."""
    ctx = _steward_ctx(term_store, person_store, embedder, viewer_member,
                       harmful="営業部の担当者が顧客の連絡先一覧をそのまま外部の生成AIに"
                               "投入し、社外へ持ち出す形になっていた", incident_flag=True)
    qid, t0, _ = _lesson(ctx)
    ev = t0["evidence"]
    minced = "、".join(ev[i:i + 18] for i in range(0, len(ev), 18))
    with pytest.raises(ValueError, match="写されている"):
        ctx.add_sfaq_feedback(qid, "decision", minced)


def test_the_back_door_refuses_a_sibling_tasks_evidence(term_store, person_store, embedder,
                                                        viewer_member):
    """兄弟タスクの evidence。裏口は元タスク1本しか見ていなかった."""
    ctx = _steward_ctx(term_store, person_store, embedder, viewer_member,
                       harmful="営業部の担当者が顧客の連絡先一覧を外部の生成AIに投入していた",
                       incident_flag=True)
    qid, _, sibling = _lesson(ctx)
    assert sibling, "前提: 同じ申告から派生した兄弟タスクがある"
    with pytest.raises(ValueError, match="写されている"):
        ctx.add_sfaq_feedback(qid, "decision", sibling["evidence"])


def test_the_back_door_refuses_names_and_contacts(term_store, person_store, embedder,
                                                  viewer_member):
    """氏名・連絡先。裏口には pii_kinds も names_in も無かった.

    これは**統合の採否理由そのもの**（「氏名は名簿で捕まえる」）が、裏口では成立して
    いなかったということ。
    """
    ctx = _steward_ctx(term_store, person_store, embedder, viewer_member,
                       harmful="担当者が資料の取り扱いを誤っていた")
    qid, _, _ = _lesson(ctx)
    with pytest.raises(ValueError, match="個人を特定できる情報"):
        ctx.add_sfaq_feedback(qid, "decision", "連絡先 sato@example.net で共有していた")

    roster = ctx.person_store.roster_names()
    assert roster
    with pytest.raises(ValueError, match="氏名"):
        ctx.add_sfaq_feedback(qid, "decision", f"{roster[0]}が独断で進めた")


def test_the_back_door_still_accepts_a_generalized_judgment(term_store, person_store,
                                                            embedder, viewer_member):
    """塞ぎすぎていないこと — 一般化された判断は積める."""
    ctx = _steward_ctx(term_store, person_store, embedder, viewer_member,
                       harmful="担当者が資料の取り扱いを誤っていた")
    qid, _, _ = _lesson(ctx)
    ok = ctx.add_sfaq_feedback(
        qid, "decision", "列名だけで匿名化仕様を確認してから渡す運用にした")
    assert ok["recorded"] is True
