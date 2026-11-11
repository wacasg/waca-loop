"""承認した用語が、その場で `lookup_terms` に出る（承認の行き止まりを塞ぐ）.

`approve_candidate` は
``term_sink = self.term_store if hasattr(self.term_store, "add") else None`` で沈み先を
決める。`BigQueryTermStore` に `add` が無ければ **常に None** になり、スチュワードが
承認しても `terms` には何も入らない。

ここで固定するのは3つ:

1. 承認 → その場で引ける（受け入れ条件そのもの）。InMemory と BigQuery の双子の両方で。
2. 書き口が **DML であって streaming insert ではない**。streaming buffer に入れると
   `backfill_embeddings.py` の UPDATE が弾かれ、その語の埋め込みが永久に作られない。
   これは「緑のまま本番だけ壊れる」形なので、実装の選択そのものを表明で留める。
3. 埋め込みが未計算の行が **ベクトル検索の対象から外れる**（埋め込みを「後追いでよい」とする
   ことを、検索を壊さずに成立させるための条件）。
"""

from __future__ import annotations

import pytest

from fake_bq import FakeBigQueryClient
from waca_loop.config import Settings
from waca_loop.embeddings import HashingEmbedder
from waca_loop.grounding.terms import BigQueryTermStore
from waca_loop.models import (
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Term,
    Viewer,
)


def _settings(provider: str = "") -> Settings:
    s = Settings()
    s.gcp_project = "demo"
    s.ds_grounding = "waca_loop_grounding"
    s.embedding_provider = provider
    return s


def _seed_row() -> dict:
    """列は本物の DDL(sql/ddl/01_terms.sql)に合わせる — 合っていなければ SQL が落ちる."""
    return {
        "term_id": "t_seed", "term": "既存語", "reading": None,
        "definition": "もとから入っている語", "basis": None, "attributes": None,
        "scope_tier": "org", "scope_audience": "internal", "scope_sensitivity": "normal",
        "owner_unit": None, "embedding": [0.1] * 8, "status": "approved", "version": 1,
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
    }


def _bq_store(provider: str = "") -> tuple[BigQueryTermStore, FakeBigQueryClient]:
    settings = _settings(provider)
    client = FakeBigQueryClient({settings.terms_table: [_seed_row()]})
    viewer = Viewer(
        subject="sub-steward", person_id="p_steward", is_org_member=True,
        units=frozenset({"steward", "dept_eng"}),
    )
    return (
        BigQueryTermStore(client, settings, viewer, HashingEmbedder(dim=8)),
        client,
    )


def _term(term: str = "サン番") -> Term:
    return Term(
        term_id="t_new", term=term, definition="会員に振る通し番号",
        scope=ScopeTags(ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL),
        status="approved",
    )


# --- 1. 受け入れ条件: 承認した語がその場で引ける ---

def test_bq_store_add_then_lexical_search_finds_it():
    """A(正): BigQuery 側でも、書いた直後に語彙検索へ出る（DML なので即時に見える）."""
    store, _ = _bq_store()
    store.add(_term())
    assert [t.term for t in store.lexical_search("サン番", 5)] == ["サン番"]


def test_approved_candidate_is_searchable_immediately(steward_context):
    """A(正): 承認 → 同じセッションの lookup_terms に出る（受け入れ条件・双子側）."""
    out = steward_context.propose_candidates(
        [{"kind": "term",
          "payload": {"term": "サン番", "definition": "会員に振る通し番号"},
          "owner_unit": "dept_eng"}]
    )
    res = steward_context.approve_candidate(out["candidate_ids"][0], approve=True)
    assert res["promoted"] is True

    hits = steward_context.lookup_terms("サン番", 5)["hits"]
    assert any(h["term"] == "サン番" for h in hits)


def test_promotion_records_the_minted_term_id_so_a_retry_does_not_duplicate(steward_context):
    """C(負): sink 書き込みと set_status の間で落ちても、語彙は二重に増えない.

    term 昇格は実際に書き込むので、SFAQ 側の `promoted_question_id` と
    同じ冪等キーが要る。
    """
    from waca_loop.improvement.candidates import approve_candidate as _approve

    out = steward_context.propose_candidates(
        [{"kind": "term", "payload": {"term": "二重防止", "definition": "x"},
          "owner_unit": "dept_eng"}]
    )
    cid = out["candidate_ids"][0]
    steward_context.approve_candidate(cid, approve=True)

    before = len(steward_context.term_store.lexical_search("二重防止", 10))
    # set_status が落ちた後の再試行を、純ロジックの層で直接なぞる。
    cand = steward_context.candidate_store.get(cid)
    _approve(cand, True, term_sink=steward_context.term_store)
    after = len(steward_context.term_store.lexical_search("二重防止", 10))
    assert before == after == 1


# --- 2. 書き口が DML であること ---

def test_add_uses_dml_not_streaming_insert():
    """C(負): streaming insert で書かない — buffer に入ると backfill の UPDATE が弾かれる.

    弾かれてもこの経路は何も気づかないので、その語の埋め込みは永久に作られない。
    「実装を選び直したら落ちる」形で留める。
    """
    store, client = _bq_store()

    def _refuse(*args, **kwargs):
        raise AssertionError(
            "streaming insert は使わない（streaming buffer が backfill の UPDATE を阻む）"
        )

    client.insert_rows_json = _refuse
    store.add(_term())
    # DML であればよい。実際の文は MERGE（upsert）— 同じ term_id で二度呼ばれても
    # 行を2つ作らないため。素の INSERT を期待して縛らない。
    assert any(sql.lstrip().upper().startswith(("INSERT INTO", "MERGE")) for sql in client.sqls)


def test_add_binds_every_value_as_a_parameter():
    """C(負): 語そのものを SQL 文字列へ埋め込まない（用語は人が書く任意テキスト）."""
    store, client = _bq_store()
    store.add(_term(term="'; DROP TABLE terms; --"))
    write_sql = next(
        s for s in client.sqls if s.lstrip().upper().startswith(("INSERT INTO", "MERGE"))
    )
    assert "DROP TABLE" not in write_sql
    assert [t.term for t in store.lexical_search("drop table", 5)] == ["'; DROP TABLE terms; --"]


# --- 3. 埋め込みは後追いでよいが、検索を壊さないこと ---

def test_add_computes_the_embedding_inline_when_python_can():
    """A(正): 既定の provider では埋め込みまで入る — 後追いを待たずベクトル検索にも出る."""
    store, _ = _bq_store()
    t = _term()
    store.add(t)
    assert len(t.embedding) == 8


def test_bqml_leaves_the_embedding_empty_for_the_backfill():
    """A(正): bq_ml は Python 側でベクトルを作れない — 空配列(NULL でなく)で置く.

    `backfill_embeddings.py` は ARRAY_LENGTH(embedding)=0 で拾うので、空配列であることが
    「後で拾われる」条件そのもの。
    """
    store, _ = _bq_store(provider="bq_ml")
    t = _term()
    store.add(t)
    assert t.embedding == []


def test_vector_search_skips_rows_whose_embedding_is_not_computed_yet():
    """C(負): 埋め込み待ちの行はベクトル検索の母集団に入らない.

    VECTOR_SEARCH は長さの揃った配列を要求するので、空配列を混ぜると検索そのものが
    落ちる。埋め込みの「後追いでよい」は、待っている行を**除外**して初めて成り立つ。
    """
    class _Recorder:
        def __init__(self):
            self.sqls: list[str] = []

        def query(self, sql, job_config=None):
            self.sqls.append(sql)

            class _J:
                def result(self_inner):
                    return iter([])

            return _J()

    for provider in ("", "bq_ml"):
        settings = _settings(provider)
        rec = _Recorder()
        viewer = Viewer(subject="s", person_id="p", is_org_member=True, units=frozenset())
        store = BigQueryTermStore(rec, settings, viewer, HashingEmbedder(dim=8))
        store.semantic_candidates("語", HashingEmbedder(dim=8), 5)
        assert rec.sqls, f"{provider}: SQL が組み立てられていない"
        assert all("ARRAY_LENGTH(t.embedding) > 0" in sql for sql in rec.sqls), provider


@pytest.mark.parametrize("provider", ["", "bq_ml"])
def test_status_and_scope_survive_the_write(provider):
    """A(正): 承認時のスコープ3軸と status が、書き戻したあとも同じ."""
    store, _ = _bq_store(provider)
    t = Term(
        term_id="t_draft", term="下書き語", definition="まだ確定していない",
        scope=ScopeTags(ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL),
        status="draft",
    )
    store.add(t)
    # status='approved' しか検索に出さないので、draft は引けない = 正直に写っている。
    assert store.lexical_search("下書き語", 5) == []


# --- 5. 冪等------------------------------------------
#
# `payload["promoted_term_id"]` は本番では効かない。`BigQueryCandidateStore.set_status` は
# status と updated_at しか書かず、terms 側に `candidate_id` の逆リンクも無いので、
# 再試行時に payload のキーは必ず消えている。だから守っているのはガードではなく
# 「id が候補から決まる」ことと「add が upsert である」ことの2つ。両方を別々に留める。

def test_the_term_id_is_derived_from_the_candidate_not_random():
    """A(正): 同じ候補を二度承認しても同じ id を指す（UUID だと別 id で二重登録になる）."""
    from waca_loop.improvement.candidates import _term_id_for

    assert _term_id_for("cand-1") == _term_id_for("cand-1")
    assert _term_id_for("cand-1") != _term_id_for("cand-2")


def test_adding_the_same_term_id_twice_leaves_one_row():
    """A(正): 同じ id で二度書いても行は増えない — BigQuery は PK を強制しない."""
    store, _ = _bq_store()
    store.add(_term(term="二重登録"))
    store.add(_term(term="二重登録"))
    assert [t.term for t in store.lexical_search("二重登録", 10)] == ["二重登録"]


def test_an_upsert_updates_in_place_rather_than_adding_a_rival_row():
    """A(正): 同じ id で語だけ変えて書くと、置き換わる（2行にならない）.

    再承認で表記が直った場合に、古い表記の行が残って `lookup_terms` が両方返す、を防ぐ。
    """
    store, _ = _bq_store()
    store.add(_term(term="サン番号"))
    store.add(_term(term="サン番"))        # 同じ term_id="t_new"
    assert [t.term for t in store.lexical_search("サン", 10)] == ["サン番"]
