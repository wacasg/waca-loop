"""E6 — サイレント非対称開示（SPEC_phase3 §3・§4f）.

ERN は**完全サイレント**。(a) 候補者側に通知する経路が存在しない、(b)「誰が探したか」を
返すツールが存在しない。

非対称なのは意図的である: 探した人は候補を知り、候補は探されたことを知らない。だから
その非対称を**どこからも覗けない**ようにしておく必要がある — 覗ける道具が1つでもあれば、
それは「誰が誰を探しているか」の一覧になる。
"""

from __future__ import annotations

import inspect

from waca_loop.server import ServerContext


def _public_methods(cls) -> list[str]:
    return [n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n))]


def test_no_tool_returns_who_searched():
    """C(負): ServerContext の公開メソッドに、検索者を返すものが無い."""
    suspicious = ("searcher", "who_searched", "asked_by", "requester", "seekers")
    for name in _public_methods(ServerContext):
        assert not any(s in name.lower() for s in suspicious), name


def test_no_tool_reads_the_ern_audit_table():
    """C(負): `ern_audit` を読み出す経路が ServerContext に無い（§4f）.

    ストア側に `list` / `search` を置かないだけでなく、サーバ側からも読まないこと。
    """
    import re

    src = inspect.getsource(ServerContext)
    # 件数を数えるのは脆い（宣言や None 判定が増えるだけで落ちる）。**性質**で見る:
    # `ern_audit_store` に対して呼ばれるメソッドは `record` だけであること。
    called = set(re.findall(r"ern_audit_store\.(\w+)", src))
    assert called == {"record"}, (
        f"ern_audit_store に record 以外が呼ばれている: {sorted(called - {'record'})} — "
        "読み出し経路が生えると『誰が誰を探したか』を返す道具になる (§4f/E6)"
    )


def test_the_audit_store_has_no_read_api():
    """C(負): 監査ストアに読み出し API が無い（InMemory / BigQuery とも）."""
    from waca_loop.ern.store import BigQueryErnAuditStore, InMemoryErnAuditStore

    for cls in (InMemoryErnAuditStore, BigQueryErnAuditStore):
        methods = set(_public_methods(cls))
        assert methods == {"record"}, (cls.__name__, methods)


def test_no_notification_path_exists():
    """C(負): 候補者へ通知する経路が ERN のどこにも無い（完全サイレント）."""
    from waca_loop.ern import engine, signals, store

    for mod in (engine, signals, store):
        src = inspect.getsource(mod)
        for bad in ("notify", "send_mail", "sendmail", "webhook", "slack", "mail("):
            assert bad not in src.lower(), f"{mod.__name__} に通知らしい呼び出し: {bad}"


def test_the_response_does_not_say_who_asked(ern_context, ern_seed_fn):
    """A(正): 応答に質問者の識別子が出ない."""
    ern_seed_fn(ern_context, authors=("p_ok",))
    out = ern_context.find_expert("採点基準")
    assert "p_asker" not in str(out)


def test_the_audit_row_is_the_only_place_the_pairing_lives(ern_context, ern_seed_fn):
    """A(正): 「誰が誰を探したか」は `ern_audit` にだけ残る（そして読み出せない）."""
    ern_seed_fn(ern_context, authors=("p_ok",))
    ern_context.find_expert("採点基準")

    rows = ern_context.ern_audit_store.rows
    assert len(rows) == 1
    assert rows[0].actor_id == "p_asker"
    assert rows[0].candidate_ids == ["p_ok"]


def test_declared_topics_never_enumerates_the_whole_profile(ern_context, ern_seed_fn):
    """C(負): `find_expert` がプロファイル列挙の道具にならない.

    宣言の全件を返していると、1回の呼び出しで、同意している任意の会員について
    「役員報酬」「解雇手続」まで含む宣言一覧が読める。宣言そのものに ACL は無く
    （`ern_assertions` は3軸を持たない）、「エキスパート探索に出てよい」と言った人が
    「宣言を全部一覧にされてよい」と言ったことにはならない。
    """
    from waca_loop.ern.store import ErnAssertion

    for i, t in enumerate(["採点基準", "役員報酬", "解雇手続"]):
        ern_context.ern_assertion_store.add(
            ErnAssertion(assertion_id=f"a{i}", person_id="p_ok", topic=t)
        )
    ern_seed_fn(ern_context, authors=("p_ok",))

    cand = ern_context.find_expert("採点基準")["candidates"][0]
    assert cand["declared_topics"] == ["採点基準"]
    assert "役員報酬" not in str(cand) and "解雇手続" not in str(cand)


def test_declared_topics_is_empty_when_the_person_never_declared_it(ern_context,
                                                                   ern_seed_fn):
    """A(正): 宣言していなければ空（「宣言した」という嘘を付けない）."""
    ern_seed_fn(ern_context, authors=("p_ok",))
    cand = ern_context.find_expert("採点基準")["candidates"][0]
    assert cand["declared_topics"] == []
