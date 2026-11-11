"""Shared fixtures: in-memory stores + sample data exercising real logic offline."""

from __future__ import annotations

import os
from pathlib import Path

# The organization profile is read once per process and unit ids are bound at import time
# (waca_loop/org.py), so point it at the test profile BEFORE any waca_loop import. A developer's
# own org.toml never leaks into the suite.
os.environ["ORG_CONFIG"] = str(Path(__file__).with_name("org.test.toml"))

import pytest

from waca_loop.embeddings import HashingEmbedder
from waca_loop.grounding.persons import InMemoryPersonStore
from waca_loop.grounding.terms import InMemoryTermStore
from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.models import (
    Person,
    PolicyRule,
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Term,
    Viewer,
)
from waca_loop.policy.store import InMemoryPolicyStore
from waca_loop.server import ServerContext


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder(dim=128)


def _scope(tier: str, audience: str, sensitivity: str) -> ScopeTags:
    return ScopeTags(ScopeTier(tier), ScopeAudience(audience), ScopeSensitivity(sensitivity))


@pytest.fixture
def sample_terms() -> list[Term]:
    return [
        Term(
            term_id="t_org",
            term="サンプル協会",
            reading="サンプルキョウカイ",
            definition="架空の協会。会員向けの研修を行う組織。",
            basis="社名・正式名称",
            scope=_scope("org", "public", "normal"),
        ),
        Term(
            term_id="t_judgment_sheet",
            term="判断記録票",
            definition="業務上の判断を記録する様式。問い・判断・理由を持つ。",
            scope=_scope("org", "internal", "normal"),
        ),
        Term(
            term_id="t_snowprism",
            term="Snowprism",
            definition="営業部の社内コードネーム製品。",
            basis="社外秘",
            owner_unit="dept_sales",
            scope=_scope("dept", "internal", "sensitive"),
        ),
        Term(
            term_id="t_color",
            term="色彩設計",
            definition="配色を決めること。",
            scope=_scope("org", "public", "normal"),
        ),
    ]


@pytest.fixture
def term_store(sample_terms, embedder) -> InMemoryTermStore:
    return InMemoryTermStore(sample_terms, embedder)


@pytest.fixture
def persons() -> list[Person]:
    return [
        Person(person_id="p_tanaka", display_name="田中", unit_path=["dept_sales"]),
        Person(person_id="p_sato", display_name="佐藤", unit_path=["dept_eng"], status="active"),
        Person(
            person_id="p_frozen",
            display_name="退職者",
            unit_path=["dept_eng"],
            status="frozen",
        ),
    ]


@pytest.fixture
def person_store(persons) -> InMemoryPersonStore:
    return InMemoryPersonStore(
        persons=persons,
        identity_map={"sub-sato": "p_sato", "sub-tanaka": "p_tanaka"},
    )


@pytest.fixture
def viewer_member() -> Viewer:
    """An org member in dept_eng (cannot see dept_sales sensitive terms)."""
    return Viewer(
        subject="sub-sato",
        person_id="p_sato",
        is_org_member=True,
        units=frozenset({"dept_eng"}),
    )


@pytest.fixture
def viewer_sales() -> Viewer:
    """An org member in dept_sales (CAN see dept_sales sensitive terms)."""
    return Viewer(
        subject="sub-tanaka",
        person_id="p_tanaka",
        is_org_member=True,
        units=frozenset({"dept_sales"}),
    )


@pytest.fixture
def viewer_outsider() -> Viewer:
    """An unresolved/external viewer — only public, non-sensitive, org-tier."""
    return Viewer.anonymous(subject="sub_hashed_outsider")


@pytest.fixture
def context(term_store, person_store, embedder, viewer_member) -> ServerContext:
    return ServerContext(
        term_store=term_store,
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=viewer_member,
        person_store=person_store,
    )


@pytest.fixture
def survey_context(term_store, person_store, embedder, viewer_member) -> ServerContext:
    """ServerContext wired with survey + improvement stores."""
    from waca_loop.improvement.candidates import InMemoryCandidateStore
    from waca_loop.improvement.tasks import InMemoryTaskStore
    from waca_loop.survey.store import InMemorySurveyStore

    return ServerContext(
        term_store=term_store,
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=viewer_member,
        person_store=person_store,
        survey_store=InMemorySurveyStore(),
        task_store=InMemoryTaskStore(),
        candidate_store=InMemoryCandidateStore(),
    )


@pytest.fixture
def steward_context(survey_context) -> ServerContext:
    """survey_context と同じ配線で、閲覧者をスチュワードにしたもの（用語昇格に必要）."""
    import dataclasses

    steward = Viewer(
        subject="sub-steward", person_id="p_steward", is_org_member=True,
        units=frozenset({"steward", "dept_eng"}),
    )
    return dataclasses.replace(survey_context, viewer=steward)


@pytest.fixture
def sample_policy_rules() -> list[PolicyRule]:
    """Org floor (2 bright-lines) + a dept level-1 rule + a trial row + a draft row.

    (SPEC_phase1 §9.6)
    """
    return [
        PolicyRule(
            row_id="FLOOR-01", scope_unit="org", is_floor=True,
            line="生の個人情報を安全でないツールに入力する",
            effect="仮名化・集計化して安全チャネル(WACA Loop)経由で扱う",
            trigger_kinds=["pii"],
        ),
        PolicyRule(
            row_id="FLOOR-02", scope_unit="org", is_floor=True,
            line="コア資産(名簿全件・試験問題全量)を丸ごと復元可能な形で外に出す",
            effect="小分け・加工して安全チャネルへ／核は手元",
            trigger_kinds=["core_asset"],
        ),
        PolicyRule(
            row_id="KYO-06", scope_unit="kyozai", level=1,
            line="受講者アンケートをAIで分析する",
            effect="仮名化してWACA Loop経由なら🟡でOK。生データ全量は出さない",
            trigger_kinds=["confidential"],
        ),
        PolicyRule(
            row_id="KYO-08", scope_unit="kyozai", level=2, status="trial",
            line="講座アンケートの自由記述を要約する", effect="公開情報のみ",
        ),
        PolicyRule(
            row_id="KYO-09", scope_unit="kyozai", level=2, status="draft",
            line="試作中の行", effect="",
        ),
    ]


@pytest.fixture
def policy_store(sample_policy_rules) -> InMemoryPolicyStore:
    return InMemoryPolicyStore(sample_policy_rules)


# --- ERN (SPEC_phase3) ----------------------------------------------------------------------
#
# E# のテストはこの4つを組み合わせて回る。**オフライン**（INV-2）— BigQuery も MCP も触らない。
#
# 人物の作り分けが要点: 同意している人 / オプトアウトした人 / 退職者 の3種を必ず置く。
# 「最強シグナルでも出ない」（E4）を、シグナルが弱いせいで通ってしまう検査にしないため。

@pytest.fixture
def ern_persons():
    """ERN 用の名簿 — 同意あり2名 ／ オプトアウト1名 ／ 退職者1名."""
    from waca_loop.models import Person

    return [
        Person(person_id="p_ok", display_name="同意 太郎",
               unit_path=["org", "kyozai"], consent_internal=True),
        Person(person_id="p_ok2", display_name="同意 次郎",
               unit_path=["org", "kyozai"], consent_internal=True),
        Person(person_id="p_optout", display_name="拒否 花子",
               unit_path=["org", "kyozai"], consent_internal=False),
        Person(person_id="p_frozen", display_name="退職 三郎",
               unit_path=["org", "kyozai"], consent_internal=True,
               status="frozen"),
    ]


@pytest.fixture
def ern_viewer():
    """ERN を呼ぶ側（org メンバー）."""
    return Viewer(
        subject="sub-asker", person_id="p_asker", is_org_member=True,
        units=frozenset({"org", "kyozai"}),
    )


@pytest.fixture
def ern_context(embedder, ern_persons, ern_viewer):
    """`find_expert` が動く最小配線。SFAQ に問い＋フィードバックを1組置く.

    問いは org/internal/normal・provenance=individual・perm_individual=True にしてある。
    corporate_training × perm_corporate × ¬perm_individual にすると **§4a の著者マスク**に
    当たって原料から落ちる — それは E4/§4a 側の専用テストで確かめる。
    """
    from waca_loop.ern.store import InMemoryErnAssertionStore, InMemoryErnAuditStore
    from waca_loop.grounding.persons import InMemoryPersonStore
    from waca_loop.knowledge.store import InMemoryQuestionStore
    from waca_loop.ledger.tool_events import InMemoryToolEventStore

    people = {p.person_id: p for p in ern_persons}
    return ServerContext(
        term_store=InMemoryTermStore([], embedder),
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=ern_viewer,
        person_store=InMemoryPersonStore(persons=ern_persons),
        knowledge_store=InMemoryQuestionStore([], embedder),
        tool_event_store=InMemoryToolEventStore(),
        ern_assertion_store=InMemoryErnAssertionStore(persons=people),
        ern_audit_store=InMemoryErnAuditStore(),
        ern_half_life_days=180,
    )


def ern_seed(ctx, *, topic="採点基準", authors=("p_ok",), fb_type=None,
             days_ago=1, provenance=None, perm_corporate=False, perm_individual=True):
    """`ctx` に「その領域の問い＋各 author のフィードバック」を1組ずつ足すヘルパ.

    テスト側から**日付とフィードバック種別を動かせる**ようにしてある — 鮮度減衰（E9）と
    重み（§2.3）は、そこを動かさないと観察できない。
    """
    from datetime import UTC, datetime, timedelta

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
    )

    qid = f"q_{topic}_{len(ctx.knowledge_store.search('', ctx.viewer, 999))}"
    ctx.knowledge_store.add_question(Question(
        question_id=qid,
        question_text=f"{topic} はどう決めるか？",
        scope=ScopeTags(ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL),
        provenance=provenance or Provenance.INDIVIDUAL,
        owner_unit="kyozai",
        attributes={"domain": [topic]},
        perm_corporate=perm_corporate,
        perm_individual=perm_individual,
        status=QuestionStatus.APPROVED,
    ))
    when = datetime.now(UTC) - timedelta(days=days_ago)
    for i, author in enumerate(authors):
        fb = Feedback(
            feedback_id=f"fb_{qid}_{i}",
            question_id=qid,
            fb_type=fb_type or FbType.DECISION,
            body="そう決めた",
            author_id=author,
        )
        ctx.knowledge_store.add_feedback(fb)
        # `add_feedback` は created_at を「いま」で上書きする（本番の双子と同じ挙動）。
        # 鮮度減衰を観察するには過去の日付が要るので、**足したあとに**戻す。
        fb.created_at = when
    return qid


@pytest.fixture
def ern_seed_fn():
    """`ern_seed` を fixture として渡す（E# の各ファイルから import せずに使えるように）."""
    return ern_seed
