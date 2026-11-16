"""B1（黒箱・MCP 契約）の**恒久スイート** — `TEST_STRATEGY.md` §2.3 / 終了条件3.

contract-tester エージェントを人が走らせるだけでは、「全ツールで緑」を主張する根拠が、
そのつど誰かが叩いたという記憶しかなくなる。個別の性質は
`test_contract_data_kinds_and_sfaq_visibility.py` などの白箱で固定し、ここでは全ツールを
機械的に叩く。

ここが引き受けるのは終了条件3の後半 — **「検査できなかったツールは『問題なし』に数えない」**。
そのために :data:`PROBES` を**名簿**として持ち、生きたツール表と**集合として一致すること**を
最初に表明する。ツールを足した人は、ここに1件書くまで赤いままになる。
沈黙して見逃すのではなく、赤くなって気づかせるのが目的。

白箱テストとの分担: ここは `ServerContext` のメソッドを**呼ばない**。FastMCP に載せた
実物へ `call_tool` で話しかけ、外から見える形（名前・必須引数・戻り値・例外の文面）だけを見る。
束ね層で外れている欠陥は、この立場からしか見えない。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from waca_loop.models import Viewer

pytestmark = pytest.mark.asyncio

# 平文で置いた「漏れてはいけない値」。例外の文面に出てこないことを確かめるための餌で、
# 実在の人物とは関係ない。氏名・連絡先・逐語のそれぞれを1つずつ用意する。
CANARY_ID = "zzq-canary-id-4417"
CANARY_NAME = "架空 太郎"
CANARY_EMAIL = "canary4417@example.invalid"
CANARY_VERBATIM = "この文は逐語であり、台帳にも例外にも出てはならない"

#: ツール名 → 黒箱から叩くときの引数。**生きたツール表と集合一致することを表明する**。
#: 値は「そのツールが受け付ける最小の引数」。副作用のあるものも含めるが、
#: 相手はすべて InMemory ストアなのでプロセス外には何も残らない。
PROBES: dict[str, dict] = {
    # --- 取得系 ---
    "lookup_terms": {"query": "サンプル協会"},
    "search_sfaq": {"query": "採点基準の決め方"},
    "find_expert": {"topic": "採点基準"},
    "get_company_context": {"query": "サンプル協会"},
    "get_personal_coach": {},
    "start_policy_workbench": {},
    "get_applicable_policy": {"task_hint": "アンケートの集計"},
    "get_pending_survey": {},
    "get_pii_anonymization_guidance": {"columns": ["氏名", "メール"]},
    "list_improvement_requests": {},
    "list_improvement_tasks": {},
    "list_withdrawn_usage": {},
    "export_my_usage": {},
    "get_withdrawn_usage": {"log_id": CANARY_ID, "reason": "audit"},
    "export_personal_recommendations": {},
    # --- 記録・宣言系 ---
    "log_usage": {
        "use_category": "l1",
        "data_kinds": ["internal"],
        "summary": "黒箱契約テストの一打",
        "client": "b1-contract",
    },
    "set_session_scope": {"scope": "org"},
    "request_log_deletion": {"log_id": CANARY_ID, "reason": "mistake"},
    "review_pending_usage": {"log_id": CANARY_ID, "action": "approve"},
    # --- 書き込み・昇格系（ゲートが効いていること自体が契約） ---
    "propose_candidates": {"items": [{"surface": "黒箱語", "definition": "テスト用"}]},
    "approve_candidate": {"candidate_id": CANARY_ID},
    "add_sfaq_question": {"question_text": "黒箱から投げた問い"},
    "add_sfaq_feedback": {"question_id": CANARY_ID, "fb_type": "supplement", "body": "補足"},
    "set_sfaq_status": {"question_id": CANARY_ID, "status": "confirmed"},
    "set_sfaq_scope": {"question_id": CANARY_ID, "tier": "org"},
    "report_improvement_request": {"kind": "bug", "title": "黒箱", "body": "本文"},
    "set_request_status": {"request_id": CANARY_ID, "status": "triaged"},
    "raise_lesson_from_task": {"task_id": CANARY_ID, "lesson_text": "学び"},
    "submit_survey_response": {
        "nps_score": 8,
        "nps_reason": "使えている",
        "useful": "用語解決",
        "harmful": "特になし",
        "incident_flag": False,
    },
}


#: ツール名 → **必須引数の集合**。生きた宣言と一致することを表明する（下のテストの経緯を参照）。
#: 引数を必須から外すのは利用者から見える契約変更なので、ここを書き換える手を要求する。
REQUIRED: dict[str, frozenset[str]] = {
    "add_sfaq_feedback": frozenset({"question_id", "fb_type", "body"}),
    "add_sfaq_question": frozenset({"question_text"}),
    "approve_candidate": frozenset({"candidate_id"}),
    "find_expert": frozenset({"topic"}),
    "get_applicable_policy": frozenset(),
    "get_company_context": frozenset({"query"}),
    "get_personal_coach": frozenset(),
    "start_policy_workbench": frozenset(),
    "get_pending_survey": frozenset(),
    "get_pii_anonymization_guidance": frozenset({"columns"}),
    "get_withdrawn_usage": frozenset({"log_id", "reason"}),
    "export_personal_recommendations": frozenset(),
    "list_improvement_requests": frozenset(),
    "list_improvement_tasks": frozenset(),
    "list_withdrawn_usage": frozenset(),
    "export_my_usage": frozenset(),
    "log_usage": frozenset({"use_category", "data_kinds", "summary", "client"}),
    "lookup_terms": frozenset({"query"}),
    "propose_candidates": frozenset({"items"}),
    "raise_lesson_from_task": frozenset({"task_id", "lesson_text"}),
    "report_improvement_request": frozenset({"kind", "title", "body"}),
    "request_log_deletion": frozenset({"log_id", "reason"}),
    "review_pending_usage": frozenset({"log_id", "action"}),
    "search_sfaq": frozenset({"query"}),
    "set_request_status": frozenset({"request_id", "status"}),
    "set_session_scope": frozenset({"scope"}),
    "set_sfaq_scope": frozenset({"question_id", "tier"}),
    "set_sfaq_status": frozenset({"question_id", "status"}),
    "submit_survey_response": frozenset(
        {"nps_score", "nps_reason", "useful", "harmful", "incident_flag"}
    ),
}


@pytest.fixture
def wired_context(term_store, person_store, embedder, viewer_member):
    """**全ツールが「未配線」で逃げない**よう、任意ストアをすべて挿した文脈.

    conftest の `context` / `survey_context` は一部しか配線していない。そのまま黒箱に使うと
    半数のツールが「not configured」で落ち、それを緑と数えてしまう —— 終了条件3 が名指しで
    禁じている「検査できなかったツールを問題なしに数える」そのものになる。
    """
    from waca_loop.guard import SessionScopeRegistry
    from waca_loop.improvement.candidates import InMemoryCandidateStore
    from waca_loop.improvement.requests import InMemoryRequestStore
    from waca_loop.improvement.tasks import InMemoryTaskStore
    from waca_loop.knowledge.store import InMemoryQuestionStore
    from waca_loop.ledger.tool_events import InMemoryToolEventStore
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore
    from waca_loop.policy import AdoptionRow
    from waca_loop.policy.memory import InMemoryAdoptionMapStore, InMemoryPolicyGuideStore
    from waca_loop.policy.store import InMemoryPolicyStore
    from waca_loop.server import ServerContext
    from waca_loop.survey.store import InMemorySurveyStore

    return ServerContext(
        term_store=term_store,
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=viewer_member,
        person_store=person_store,
        survey_store=InMemorySurveyStore(),
        task_store=InMemoryTaskStore(),
        request_store=InMemoryRequestStore(),
        candidate_store=InMemoryCandidateStore(),
        policy_store=InMemoryPolicyStore([]),
        policy_guide_store=InMemoryPolicyGuideStore(),
        adoption_map_store=InMemoryAdoptionMapStore([
            AdoptionRow(
                row_id="b1-row",
                scope_unit="dept_eng",
                actor_scope=f"person:{viewer_member.person_id}",
                business="黒箱契約テスト",
                current_level=1,
                target_level=2,
                share_scope="person",
            )
        ]),
        knowledge_store=InMemoryQuestionStore(embedder=embedder),
        scope_registry=SessionScopeRegistry(),
        tool_event_store=InMemoryToolEventStore(),
    )


def _server(ctx):
    pytest.importorskip("mcp")
    from waca_loop.server import build_mcp

    return build_mcp(ctx)


async def _roster(ctx) -> dict[str, dict]:
    tools = await _server(ctx).list_tools()
    return {t.name: (t.inputSchema or {}) for t in tools}


async def _call(ctx, name: str, args: dict) -> tuple[object, Exception | None]:
    """ツールを叩き、payload か例外を返す。**どちらも「観察できたこと」として扱う**。"""
    try:
        out = await _server(ctx).call_tool(name, args)
    except Exception as exc:  # noqa: BLE001 — 例外の文面そのものが検査対象
        return None, exc
    if isinstance(out, tuple):  # 新しい SDK は (content, structured) を返す
        out = out[0]
    texts = [c.text for c in out if getattr(c, "text", None)]
    try:
        return json.loads("\n".join(texts)), None
    except (ValueError, TypeError):
        return "\n".join(texts), None


# --- 終了条件3の本体 ------------------------------------------------------------


async def test_every_live_tool_has_a_probe(wired_context):
    """名簿と生きたツール表が**集合として一致**すること（検査漏れを緑にしない）."""
    live = set(await _roster(wired_context))
    named = set(PROBES)
    assert live == named, (
        "B1 の名簿とツール表がずれています。\n"
        f"  名簿に無い（＝検査されないまま緑になる）: {sorted(live - named)}\n"
        f"  もう存在しない: {sorted(named - live)}\n"
        "ツールを足したら PROBES に1件書いてください（終了条件3）。"
    )


@pytest.mark.parametrize("tool", sorted(PROBES))
async def test_every_tool_answers_or_refuses_cleanly(wired_context, tool):
    """全ツールが**落ちずに**応じること — 素通り・500・未配線のどれでもない。

    ゲートに弾かれるのは正しい振る舞いなので、例外そのものは失格にしない。失格にするのは
    「未配線だから動かない」— それは検査できていないという意味であり、緑に数えてはいけない。
    """
    payload, exc = await _call(wired_context, tool, PROBES[tool])
    if exc is not None:
        assert "not configured" not in str(exc), (
            f"{tool} が未配線で落ちています。これは『検査できなかった』であって緑ではない"
        )
        return
    assert payload is not None, f"{tool} が何も返していない"


@pytest.mark.parametrize("tool", sorted(PROBES))
async def test_the_required_argument_set_has_not_shrunk(wired_context, tool):
    """必須引数の集合が :data:`REQUIRED` と**一致**すること（黙って緩まないように）.

    「宣言された必須を1つずつ省いて落ちるか」だけを見て、必須が無ければ `skip` する形だと、
    **必須を外す変異を当てると `required` が空になり、テストが自分を skip して緑になる** ——
    守るはずの当のものが消えた瞬間に番人も消える。だから期待値は**外に固定**する。緩めるときは、ここを書き換える手が要る。
    """
    schema = (await _roster(wired_context))[tool]
    assert set(schema.get("required", [])) == REQUIRED[tool], (
        f"{tool} の必須引数が宣言と違います。"
        f"実物={sorted(schema.get('required', []))} / 期待={sorted(REQUIRED[tool])}"
    )


@pytest.mark.parametrize("tool", sorted(t for t, r in REQUIRED.items() if r))
async def test_omitting_a_required_argument_is_refused(wired_context, tool):
    """必須と宣言した引数を省いたら**断られる**こと（宣言と実装のずれを外から見る）."""
    for missing in sorted(REQUIRED[tool]):
        args = {k: v for k, v in PROBES[tool].items() if k != missing}
        _, exc = await _call(wired_context, tool, args)
        assert exc is not None, (
            f"{tool}: 必須と宣言している {missing!r} を省いても通ってしまう"
        )


# --- 漏れの検査（例外の文面・ペルソナ） ------------------------------------------


@pytest.mark.parametrize("tool", sorted(PROBES))
async def test_error_text_never_quotes_the_value_it_was_given(wired_context, tool):
    """例外の文面に**渡した値そのもの**が混ざらないこと（§2.3 4点目）.

    `log_id` のような識別子は本人が投げたものなので出ても実害は小さいが、氏名・連絡先・逐語は
    別。ここでは後者3つだけを禁じる — 禁止を広げすぎると通らないテストになり、消される。
    """
    args = dict(PROBES[tool])
    for key in ("summary", "body", "lesson_text", "question_text", "title", "reason_summary"):
        if key in args:
            args[key] = CANARY_VERBATIM
    for key in ("useful", "harmful", "nps_reason"):
        if key in args:
            args[key] = CANARY_VERBATIM

    payload, exc = await _call(wired_context, tool, args)
    text = str(exc) if exc is not None else json.dumps(payload, ensure_ascii=False, default=str)
    for canary, label in (
        (CANARY_VERBATIM, "逐語"),
        (CANARY_NAME, "氏名"),
        (CANARY_EMAIL, "連絡先"),
    ):
        assert canary not in text, f"{tool} の応答／例外に{label}が出ています"


@pytest.mark.parametrize("tool", sorted(PROBES))
async def test_an_outsider_is_never_handed_more_than_a_member(wired_context, tool):
    """部外者の応答が、組織メンバーの応答を**超えない**こと（ペルソナ別の観察）.

    件数で見る。中身の比較はストアごとの白箱（W2）の仕事で、ここは「外から叩いたら
    部外者のほうが多く返ってきた」という束ね層の欠陥だけを捕まえる。

    **この検査が比較まで届くのは一部のツールだけ**（残りはゲートや例外で返る経路）。
    全ツールを漏れ検査したとは読まないこと。届いていることの担保は次のテスト。
    """
    outsider = dataclasses.replace(
        wired_context, viewer=Viewer.anonymous(subject="sub_hashed_outsider")
    )
    member_payload, _ = await _call(wired_context, tool, PROBES[tool])
    outsider_payload, _ = await _call(outsider, tool, PROBES[tool])
    if not isinstance(member_payload, dict) or not isinstance(outsider_payload, dict):
        return
    for key, mine in member_payload.items():
        theirs = outsider_payload.get(key)
        if isinstance(mine, list) and isinstance(theirs, list):
            assert len(theirs) <= len(mine), (
                f"{tool}.{key}: 部外者に {len(theirs)} 件、メンバーに {len(mine)} 件 — 逆転している"
            )


async def test_no_tool_declares_a_verbatim_field(wired_context):
    """どのツールの**宣言**にも逐語の入れ物が無いこと（§2.2③ / T6 を外から）.

    白箱の T6 はモデル・行・SQL を見る。ここは**利用者から見える宣言**を見る — クライアントに
    「逐語を入れる場所がある」と教えてしまえば、ストアが持たなくても意味がない。
    """
    banned = ("prompt", "raw_text", "transcript", "verbatim", "raw_prompt", "messages")
    for name, schema in (await _roster(wired_context)).items():
        for field in (schema.get("properties") or {}):
            assert field.lower() not in banned, f"{name} が逐語の引数 {field!r} を宣言している"


#: 閲覧者ゲートを持つ取得系。ここが比較に届かなくなったら、上のテストは**素通りしている**。
ACL_BEARING = ("lookup_terms", "search_sfaq", "get_company_context", "get_applicable_policy")


@pytest.mark.parametrize("tool", ACL_BEARING)
async def test_the_persona_comparison_actually_reaches_these_tools(wired_context, tool):
    """ゲート持ちの取得系で、**比較する list が実在する**こと（上のテストの空回り防止）.

    上の検査は「dict でなければ黙って戻る」形なので、応答の形が変わると誰にも気づかれずに
    無検査になる。この形のテストは飾りになりやすいので、届いていること自体を
    別の表明にしておく。
    """
    outsider = dataclasses.replace(
        wired_context, viewer=Viewer.anonymous(subject="sub_hashed_outsider")
    )
    mine, _ = await _call(wired_context, tool, PROBES[tool])
    theirs, _ = await _call(outsider, tool, PROBES[tool])
    assert isinstance(mine, dict) and isinstance(theirs, dict), f"{tool} が dict を返していない"
    compared = [
        k for k, v in mine.items() if isinstance(v, list) and isinstance(theirs.get(k), list)
    ]
    assert compared, f"{tool}: 件数を比較できる list が応答に無い — 漏れ検査が空回りしている"
