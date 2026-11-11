"""S6 — ACL: incident は機微スコープで記録される."""

from __future__ import annotations

from waca_loop.survey.store import incident_sensitivity


def test_incident_response_tagged_sensitive(survey_context):
    survey_context.submit_survey_response(
        nps_score=5,
        nps_reason="",
        useful="",
        harmful="",
        incident_flag=True,
        incident_summary="顧客名簿がAI出力に混入",
    )
    row = survey_context.survey_store.rows[0]
    assert row["scope_sensitivity"] == "sensitive"
    # owner_unit is carried so the ACL gate (§3.3) can restrict who sees it.
    assert row["owner_unit"] == "dept_eng"


def test_non_incident_is_normal(survey_context):
    survey_context.submit_survey_response(
        nps_score=9, nps_reason="", useful="", harmful="", incident_flag=False
    )
    assert survey_context.survey_store.rows[0]["scope_sensitivity"] == "normal"


def test_incident_never_lands_on_an_org_wide_owner(term_store, person_store, embedder):
    """複数ユニット所属者の incident が org 所有で着地しない (§3.3).

    `next(iter(viewer.units))` で決めると、`org` は全員の unit_path に入るため
    **機微なインシデント申告が全社可視で着地しうる**（sensitive ゲートは
    `owner_unit in viewer.units` であり、org は全員が持つ）。派生する改善課題にも
    伝播する。ここでは全員が持つユニットに落ちないことと、曖昧なら誰にも
    見えない側へ倒れることを固定する。
    """
    import dataclasses

    from waca_loop.improvement.acl import task_visible_to
    from waca_loop.improvement.candidates import InMemoryCandidateStore
    from waca_loop.improvement.tasks import InMemoryTaskStore
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore
    from waca_loop.models import Viewer
    from waca_loop.server import ServerContext
    from waca_loop.survey.store import InMemorySurveyStore

    multi = Viewer(subject="s", person_id="p_yamada", is_org_member=True,
                   units=frozenset({"secretariat", "kyozai", "org"}))
    ctx = ServerContext(
        term_store=term_store, usage_store=InMemoryUsageLogStore(), embedder=embedder,
        viewer=multi, person_store=person_store, survey_store=InMemorySurveyStore(),
        task_store=InMemoryTaskStore(), candidate_store=InMemoryCandidateStore(),
    )
    ctx.submit_survey_response(
        nps_score=3, nps_reason="", useful="", harmful="",
        incident_flag=True, incident_summary="機微な申告",
    )
    row = ctx.survey_store.rows[0]
    assert row["scope_sensitivity"] == "sensitive"
    assert row["owner_unit"] != "org"     # 全員が持つユニット = 実質全社可視
    assert row["owner_unit"] == "steward"  # 曖昧ならスチュワードへ寄せる

    # 無関係な同僚の改善課題一覧に、この incident が出てこない
    # (NPS detractor 等の非 incident 課題は組織内共有で正しく見える — そちらは対象外)
    stranger = Viewer(subject="s2", person_id="p_other", is_org_member=True,
                      units=frozenset({"tech", "org"}))
    visible = [t for t in ctx.task_store.list(status=None, kind=None, viewer=stranger)
               if task_visible_to(t, stranger)]
    assert [t for t in visible if t["kind"] == "incident"] == []

    # 申告は失われず、スチュワードには届く
    steward = Viewer(subject="s3", person_id="p_tanaka", is_org_member=True,
                     units=frozenset({"steward", "tech", "org"}))
    assert any(t["kind"] == "incident"
               for t in ctx.task_store.list(status=None, kind=None, viewer=steward)
               if task_visible_to(t, steward))

    # 所属ユニットを明示すれば、その部門に正しく紐づく
    ctx2 = dataclasses.replace(ctx, survey_store=InMemorySurveyStore(),
                               task_store=InMemoryTaskStore())
    ctx2.submit_survey_response(
        nps_score=3, nps_reason="", useful="", harmful="", incident_flag=True,
        incident_summary="機微な申告", owner_unit="secretariat",
    )
    assert ctx2.survey_store.rows[0]["owner_unit"] == "secretariat"


def test_incident_owner_unit_cannot_be_another_unit(survey_context):
    """(負): 所属していないユニットを指定しても、そこには紐づかない."""
    survey_context.submit_survey_response(
        nps_score=3, nps_reason="", useful="", harmful="", incident_flag=True,
        incident_summary="機微な申告", owner_unit="secretariat",  # viewer は dept_eng
    )
    assert survey_context.survey_store.rows[0]["owner_unit"] == "steward"


def test_incident_sensitivity_helper():
    assert incident_sensitivity(True).value == "sensitive"
    assert incident_sensitivity(False).value == "normal"


def test_escalate_routes_past_the_reporters_own_unit(survey_context):
    """自部門に当事者がいる申告を、自部門を飛ばしてスチュワードへ送れること.

    既定の届け先は申告者の所属部門であり、人事上の申告ではそこに当事者(多くは上長)が
    いることが普通にある。自分の系統を迂回する経路が無ければ、この窓口は使えない。
    """
    survey_context.submit_survey_response(
        nps_score=3, nps_reason="", useful="", harmful="", incident_flag=True,
        incident_summary="機微な申告", escalate=True,
    )
    row = survey_context.survey_store.rows[0]
    assert row["owner_unit"] == "steward"     # dept_eng を飛ばした
    assert row["scope_sensitivity"] == "sensitive"


def test_escalate_intent_is_persisted_on_the_row(survey_context):
    """迂回の意思そのものが行に残ること.

    owner_unit だけでは「解決できなかったので既定の届け先になった」のか
    「申告者が自部門を意図的に外した」のかが事後に区別できず、スチュワードが
    申告者の部門へ差し戻す余地が残る。escalated はその意思の記録である。
    """
    survey_context.submit_survey_response(
        nps_score=3, nps_reason="", useful="", harmful="", incident_flag=True,
        incident_summary="機微な申告", escalate=True,
    )
    assert survey_context.survey_store.rows[0]["escalated"] is True


def test_non_escalated_response_records_the_absence_of_intent(survey_context):
    """既定経路の申告は escalated=False で入る(NULL/欠落にしない)."""
    survey_context.submit_survey_response(
        nps_score=8, nps_reason="", useful="便利", harmful="", incident_flag=False,
    )
    assert survey_context.survey_store.rows[0]["escalated"] is False


def test_escalate_overrides_an_explicit_owner_unit(survey_context):
    """escalate は owner_unit の指定より強い(迂回の意思を取りこぼさない)."""
    survey_context.submit_survey_response(
        nps_score=3, nps_reason="", useful="", harmful="", incident_flag=True,
        incident_summary="機微な申告", owner_unit="dept_eng", escalate=True,
    )
    assert survey_context.survey_store.rows[0]["owner_unit"] == "steward"


def test_reporter_is_told_where_it_went(survey_context):
    """届け先を返す — 申告者に「誰が見るか」を伝えられるようにする."""
    out = survey_context.submit_survey_response(
        nps_score=3, nps_reason="", useful="", harmful="", incident_flag=True,
        incident_summary="機微な申告",
    )
    assert out["owner_unit"] == "dept_eng"


def test_escalate_without_incident_flag_still_stays_out_of_the_org_feed(survey_context,
                                                                       viewer_sales):
    """(負): escalate だけ立てて incident_flag を落としても全社に出ないこと.

    申告の本文は harmful に入り、escalate だけが立つことが現実にありうる。sensitivity が
    normal のままだと、派生した harmful タスクが owner_unit 無しで**組織全体に読める**状態で
    着地する — 申告先から外したはずの当事者を含めて。escalate 自体が「これは機微だ」の表明である。
    """
    from waca_loop.improvement.acl import task_visible_to

    out = survey_context.submit_survey_response(
        nps_score=5, nps_reason="", useful="", harmful="上長から繰り返し詰められている",
        incident_flag=False, escalate=True,
    )
    assert out["owner_unit"] == "steward"
    assert survey_context.survey_store.rows[0]["scope_sensitivity"] == "sensitive"

    # 別部門の一般メンバーには、この申告由来のタスクが1件も見えない
    rows = survey_context.task_store.list(status=None, kind=None, viewer=viewer_sales)
    assert [t for t in rows if task_visible_to(t, viewer_sales)] == []

    # スチュワードには届く
    from waca_loop.models import Viewer
    steward = Viewer(subject="s9", person_id="p_tanaka", is_org_member=True,
                     units=frozenset({"steward", "org"}))
    got = [t for t in survey_context.task_store.list(status=None, kind=None, viewer=steward)
           if task_visible_to(t, steward)]
    assert got and all(t["owner_unit"] == "steward" for t in got)
