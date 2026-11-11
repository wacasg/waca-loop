"""W2 双子適合 — InMemory と BigQuery が同じ振る舞いをすることを、**メソッドごとに手で
書くのではなく性質として**検査する（docs/TEST_STRATEGY.md §2.2）。

なぜ性質で書くか。重い欠陥の多くは「BigQuery 側にだけあるコード」に住む。オフラインの
スイートはそこを1行も実行しない。個別に気づいて個別にテストを足すやり方では取りこぼす
（`roster_names` の実装漏れ、`feedback_for` の無ゲート、など）。

だからここでは**新しいメソッドが増えても自動的に検査対象になる**書き方をする。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re

import pytest

import waca_loop
from waca_loop.models import Viewer


def _discover_pairs() -> list[tuple[type, type]]:
    """``InMemoryX`` / ``BigQueryX`` の対を**パッケージ全体から自動で見つける**。

    対の一覧を手で書くと、書き忘れた対が検査から漏れる（`candidates` / `tasks` / `survey` /
    `policy` / `client_store` / `tool_events` のように）。
    新しいストアを足した人が、このファイルの存在を知らなくても検査対象になるようにする。
    """
    pairs: dict[str, dict[str, type]] = {}
    for mod in pkgutil.walk_packages(waca_loop.__path__, "waca_loop."):
        try:
            m = importlib.import_module(mod.name)
        except Exception:   # noqa: BLE001 — 読み込めないモジュールは他のテストが見る
            continue
        for attr in dir(m):
            for prefix in ("InMemory", "BigQuery"):
                if attr.startswith(prefix) and isinstance(getattr(m, attr), type):
                    pairs.setdefault(attr[len(prefix):], {})[prefix] = getattr(m, attr)
    return [
        (v["InMemory"], v["BigQuery"])
        for _, v in sorted(pairs.items())
        if "InMemory" in v and "BigQuery" in v
    ]


PAIRS = _discover_pairs()
BQ_STORES = [bq for _, bq in PAIRS]


def test_the_discovery_actually_finds_the_pairs():
    """検査の空振りを防ぐ。対が減ったら気づけるようにしておく。"""
    assert len(PAIRS) >= 10, f"双子の対が {len(PAIRS)} 組しか見つからない: {PAIRS}"

# 片方にしか無いことが**意図されている**メソッド。書くこと自体が理由を残す場になる。
KNOWN_ASYMMETRY: dict[tuple[str, str], set[str]] = {
    # 用語の投入は seed スクリプト経由（§2.2）。本番ストアに add は置かない
    # — 能力ベースのゲートが「本番でだけ開く」形になるのを避けるため。
    ("InMemoryTermStore", "BigQueryTermStore"): {"add"},
    # ポリシー行の投入は seed スクリプト経由（正典シート → sync_policy_from_sheet →
    # seed_policy）。本番ストアに add は置かない
    # — WACA Loop がポリシーを書かない境界（SPEC_phase1 §9.6）。
    ("InMemoryPolicyStore", "BigQueryPolicyStore"): {"add"},
    # **注意（暫定の非対称）**: InMemoryCandidateStore.list は本番側に無い。
    # いまは誰も呼んでいない（server.py は get のみ）ので害は出ていないが、
    # 「保留中の候補を一覧する」ツールを足した人が本番でだけ落ちる罠として残っている。
    # 対にするか、InMemory 側から消すかの判断が要る。
    ("InMemoryCandidateStore", "BigQueryCandidateStore"): {"list"},
}


def _public(cls: type) -> set[str]:
    return {n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n, None))}


@pytest.mark.parametrize(("im", "bq"), PAIRS, ids=lambda c: c.__name__)
def test_w2_1_the_twins_expose_the_same_methods(im, bq):
    """W2-1: 公開メソッドの集合が一致する."""
    allowed = KNOWN_ASYMMETRY.get((im.__name__, bq.__name__), set())
    only_im = _public(im) - _public(bq) - allowed
    only_bq = _public(bq) - _public(im) - allowed
    assert not only_im, (
        f"{im.__name__} にしか無い: {sorted(only_im)} — 本番は BigQuery 側なので、"
        "ここが空でないとオフライン緑のまま本番で AttributeError になる"
    )
    assert not only_bq, f"{bq.__name__} にしか無い: {sorted(only_bq)}"


@pytest.mark.parametrize(("im", "bq"), PAIRS, ids=lambda c: c.__name__)
def test_w2_1b_shared_methods_take_the_same_arguments(im, bq):
    """引数名まで一致していること。片方だけ引数が増えると呼び分けが要る＝双子でなくなる。"""
    for name in sorted(_public(im) & _public(bq)):
        a = list(inspect.signature(getattr(im, name)).parameters)
        b = list(inspect.signature(getattr(bq, name)).parameters)
        assert a == b, f"{name}: {im.__name__}{a} と {bq.__name__}{b} で引数が違う"


@pytest.mark.parametrize(("im", "bq"), PAIRS, ids=lambda c: c.__name__)
def test_w2_3_acl_arguments_never_have_a_default(im, bq):
    """W2-3: `viewer` に既定値を持たせない（`feedback_for(viewer=None)` の形）.

    既定値があると、呼び忘れた新しい経路で ACL ゲートが**黙って**無効になる。
    fail closed にできない引数は、そもそも省略させない。
    """
    for cls in (im, bq):
        for name in sorted(_public(cls)):
            params = inspect.signature(getattr(cls, name)).parameters
            v = params.get("viewer")
            if v is None or v.kind is inspect.Parameter.VAR_KEYWORD:
                continue
            if v.default is inspect.Parameter.empty:
                continue
            # None 既定を許すのは「閲覧者を取らないこともある」検索系だけ。
            # 読み取りが行を返すメソッドで None 既定は不可。
            assert name in _VIEWER_OPTIONAL_BY_DESIGN, (
                f"{cls.__name__}.{name} の viewer に既定値 {v.default!r} がある。"
                "省略できる ACL ゲートは、いつか省略される"
            )


# 閲覧者を省略できることが設計として正しいもの。増やすときは理由をここに書く。
_VIEWER_OPTIONAL_BY_DESIGN = {
    # persons.search: viewer=None は「何も返さない」= fail closed（§3.3）。
    "search",
    # set_scope: viewer を渡すと SQL 側にもゲートが増える“上乗せ”で、省略しても
    # 呼び出し側のゲートは残る（knowledge/store.py の docstring 参照）。
    "set_scope",
}


VIEWER = Viewer(subject="s", person_id="p", is_org_member=True, units=frozenset({"org"}))


# --- W2-2: 閲覧者を取る BigQuery の読み取りは、SQL にゲートを入れる ------------------
#
# `feedback_for` に viewer も SQL ゲートも無いと、question_id さえ分かれば誰の判断でも本文と
# author_id が取れる。それを**メソッドを名指しせずに**捕まえる仕掛け。
# 新しい読み取りメソッドが増えても自動的に検査対象になる。


class _RecordingJob:
    def __init__(self):
        self.num_dml_affected_rows = 1

    def result(self):
        return iter(())


class _RecordingClient:
    """SQL を記録するだけの偽 BigQuery クライアント（行は返さない）。"""

    def __init__(self):
        self.sqls: list[str] = []

    def query(self, sql, job_config=None):
        self.sqls.append(sql)
        return _RecordingJob()

    def insert_rows_json(self, table, rows):
        return []


class _StubEmbedder:
    """embed するだけのスタブ（ベクトルの中身は検査しない）。"""

    def embed(self, text: str) -> list[float]:
        return [0.0] * 8


# 書き込み系。ACL ゲートの形が読み取りと違うので、この性質の対象外にする。
_WRITES = {"add", "add_question", "add_feedback", "set_status", "set_scope",
           "delete_questions", "record", "add_candidate", "upsert"}


class _Settings:
    """設定のスタブ。**`*_table` は機械的に生やす** — 新しいストアが新しいテーブルを
    参照するたびにここを直す必要があると、直し忘れて検査が落ちる（＝検査が邪魔者になり、
    allowlist に逃がされる）。属性名から表名を作って返す。"""

    def __getattr__(self, name: str) -> str:
        if name.endswith("_table") or name.endswith("_ref"):
            return f"p.d.{name.removesuffix('_table').removesuffix('_ref')}"
        raise AttributeError(name)

    gcp_project = "p"
    persons_table = "p.d.persons"
    terms_table = "p.d.terms"
    questions_table = "p.d.questions"
    sfaq_feedback_table = "p.d.feedback"
    usage_log_table = "p.d.usage_log"
    identity_map_table = "p.d.oauth_identity_map"
    embedding_model_ref = "p.d.embedding_model"
    embedding_provider = "hashing"
    embedding_dim = 8


def _dummy_for(name: str, annotation) -> object:
    """引数名から、その場しのぎの値を作る（呼べればよい。返り値は見ない）。"""
    from waca_loop.models import QuestionStatus, ScopeSensitivity, ScopeTier

    if name == "viewer":
        return VIEWER
    if name == "embedder":
        return _StubEmbedder()
    if name == "tier":
        return ScopeTier.ORG
    if name == "sensitivity":
        return ScopeSensitivity.NORMAL
    if name == "status":
        return QuestionStatus.PROVISIONAL
    text = str(annotation)
    if name in ("query_vector", "vector", "qvec", "embedding"):
        return [0.0] * 8
    if "float" in text:
        return [0.0] * 8
    if "list" in text or name.endswith("_ids"):
        return ["x"]
    if "int" in text or name in ("limit", "k", "days", "top_k"):
        return 3
    if "bool" in text:
        return False
    return "x"


# where 句が使われうる別名。ストアが使っているものを列挙する（増えたら足す）。
_ALIASES = (None, "p", "q", "f", "t", "m", "c", "s", "r")


def _gate_fragments() -> tuple[str, ...]:
    """ACL ゲートが SQL に入っていることの目印 = **句そのもの（全文）**。

    パラメータ名の部分一致で見るのは**ごまかせる**: `@acl_is_org_member` が1つ入って
    いれば、保護対象の行に対応していない述語でも合格してしまう（`feedback_for_many` から
    行対応の `EXISTS` を外し `AND @acl_is_org_member` だけ残す変異でも、テストは緑のまま
    漏れる）。
    「ACL らしい語がある」と「見えない行が DB から出ない」は別のことである。

    そこで**句の全文**で照合する。句は別名（`p.` / `q.` / `f.`）付きで埋め込まれるので、
    ありうる別名すべてで生成して、そのいずれかが丸ごと含まれることを求める。
    層ごとに where 句生成関数が別にあるので、新しい層を足したらここにも足すこと。
    """
    from waca_loop.improvement.acl import task_acl_where_clause
    from waca_loop.improvement.requests import request_acl_where_clause
    from waca_loop.knowledge.acl import question_where_clause
    from waca_loop.policy.acl import policy_where_clause
    from waca_loop.retrieval.acl import acl_where_clause, person_where_clause

    out: list[str] = []
    for fn in (acl_where_clause, person_where_clause, question_where_clause,
               policy_where_clause, task_acl_where_clause, request_acl_where_clause):
        for alias in _ALIASES:
            try:
                sql, _ = fn(VIEWER) if alias is None else fn(VIEWER, alias=alias)
            except TypeError:
                continue
            if sql:
                out.append(sql)
    assert out, "ゲート句を1つも生成できない（検査が空振り）"
    return tuple(out)


def _correlates_rows(sql: str) -> bool:
    """ゲートが**読んでいる行に対応している**か。

    副問い合わせの中にゲートを置く形（`EXISTS (SELECT 1 FROM questions AS q WHERE
    q.question_id = f.question_id AND <gate>)`）では、対応の等式が無ければ「どこかに
    見える行が1つでもあれば通る」になり、ゲートの意味が消える。
    """
    if "EXISTS" not in sql.upper():
        return True   # 主問い合わせの WHERE に直接入っている形
    body = sql[sql.upper().index("EXISTS"):]
    return bool(re.search(r"\b\w+\.\w+\s*=\s*\w+\.\w+", body))


# 閲覧者ゲートの要る読み取りを持たないストア。増やすときは理由をここに書く。
_NO_VIEWER_GATED_READS = {
    # ERN の監査証跡。**読み出す MCP ツールを作らない**のが仕様そのもの（SPEC_phase3 §4f）—
    # 作った瞬間「誰が誰を探したか」を返す道具が1つでき、E6 のサイレント非対称開示が
    # 台帳経由で破れる。だから viewer ゲートの読みが「無い」ことが正しい状態であり、
    # ここに読み取りが増えたらこのテストが落ちる（= 気づける）。
    "BigQueryErnAuditStore",
    # 証跡の読み出しは2経路。(a) scripts/coverage_report.py の集計、(b) C4 nudge の
    # recent_by_actor。(b) は閲覧者由来だが、門番は
    # `viewer` オブジェクトではなく **actor 述語**（ledger_actor の actor_id/actor_raw）で、
    # W2-2 の署名検査には引っかからない。ACL としては二重化してあり、SQL の述語と
    # ServerContext._coverage_hint の再フィルタが対（§3.3 の流儀）。ここを外すときは
    # その二重化ごと見直すこと。
    "BigQueryToolEventStore",
    # スコープ宣言は「その人自身の宣言」しか読み書きしない。門番は viewer オブジェクトでは
    # なく **actor 述語**（ledger_actor の actor_id/actor_raw）で、tool_events と同じ形。
    # 他人の宣言に届く引数がそもそも無く、呼び出し側（ServerContext）は自分の viewer からしか
    # actor を作らない。返すのは自分の scope と宣言時刻だけで、他 viewer の行は SQL から出ない。
    "BigQuerySessionScopeStore",
    # 候補は起票と承認だけ。承認は steward 限定で ServerContext 側が門番。
    "BigQueryCandidateStore",
    # アンケートの回答済み判定はトークン単位で、閲覧者の可視範囲とは別軸（S1〜S3）。
    "BigQuerySurveyStore",
    # OAuth クライアント登録は認証前の DCR。閲覧者がまだ存在しない層。
    "BigQueryOAuthClientStore",
}

# ゲートを SQL に入れないことが設計として正しいもの。増やすときは理由をここに書く。
_UNGATED_BY_DESIGN = {
    # 名簿の否定判定用。全件見るのが検査として正しく、絞ると漏れる側に倒れる（§5 参照）。
    "roster_names",
    # 台帳への書き込み。閲覧者は行の属性であってゲートではない。
    "record",
    # 候補→問いの逆リンク。**書き込み経路のガード**であって読み取りではなく、返すのは
    # id ひとつで内容を返さない（SPEC_phase2 §2.4 / S8）。ただし id は書き込みハンドルに
    # なりうるので、**呼び出し側が可視性を確かめてからでないと外へ出さない**
    # （`approve_candidate` の `question_id_withheld`）。ここを読み取り扱いにして
    # ゲートを足すと、冪等性の判定が「見える人にだけ効く」= 二重昇格が起きる。
    "find_by_candidate_id",
}


@pytest.mark.parametrize("bq", BQ_STORES, ids=lambda c: c.__name__)
def test_w2_2_viewer_taking_reads_put_the_gate_in_the_sql(bq):
    """W2-2: viewer を受け取る BigQuery メソッドは、生成 SQL に ACL ゲートを含む.

    §3.3 の二重ゲートは「見えない行はそもそもDBから出ない」半分と「取ってから再確認する」
    半分でできている。呼び出し側が先に可視性を確かめているから安全、では前者を欠いた
    ままで、question_id を直接受け取るツールが1つ増えた瞬間に漏れる。
    """
    fragments = _gate_fragments()
    # 双子の作りは2通りある。閲覧者を**コンストラクタで束ねる**型（terms / questions —
    # ホステッド版が毎リクエスト作り直す形）と、**メソッド引数で受ける**型（persons）。
    # どちらでも同じ性質を検査する。
    ctor = inspect.signature(bq.__init__).parameters
    viewer_in_ctor = "viewer" in ctor

    def _build(client):
        if viewer_in_ctor:
            return bq(client, _Settings(), VIEWER, _StubEmbedder())
        return bq(client, _Settings())

    checked: list[str] = []
    uncallable: list[str] = []
    for name in sorted(_public(bq)):
        if name in _UNGATED_BY_DESIGN:
            continue
        fn = getattr(bq, name)
        params = inspect.signature(fn).parameters
        takes_viewer = "viewer" in params
        if not (takes_viewer or viewer_in_ctor):
            continue
        if viewer_in_ctor and not takes_viewer and name in _WRITES:
            continue   # 書き込みは別の性質（W2-4）で見る
        client = _RecordingClient()
        store = _build(client)
        args, kwargs = [], {}
        for pname, p in params.items():
            if pname == "self":
                continue
            if p.kind is p.KEYWORD_ONLY:
                kwargs[pname] = _dummy_for(pname, p.annotation)
            elif p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                args.append(_dummy_for(pname, p.annotation))
        try:
            getattr(store, name)(*args, **kwargs)
        except Exception as exc:   # noqa: BLE001
            # **skip にしない。** 呼べなかったものを飛ばすと「検査していない」が
            # 「問題なし」として緑になる — 未検査を安全と報告するのがこの手の
            # 仕掛けの典型的な壊れ方（docs/TEST_STRATEGY.md、contract-tester 定義）。
            uncallable.append(f"{name}: {type(exc).__name__} {exc}")
            continue
        assert client.sqls, f"{bq.__name__}.{name} が SQL を1本も投げていない"
        gated = [sql for sql in client.sqls if any(fr in sql for fr in fragments)]
        assert gated, (
            f"{bq.__name__}.{name} は viewer を受け取りながら、生成 SQL に ACL ゲート句が"
            "**丸ごと**は入っていない。ACL らしいパラメータが1つあるだけでは、"
            "保護対象の行に対応しない述語でも通ってしまう（§3.3 の片側が欠ける）。\n"
            + "\n".join(client.sqls)
        )
        for sql in gated:
            assert _correlates_rows(sql), (
                f"{bq.__name__}.{name}: ゲートが副問い合わせの中にあるのに、"
                "読んでいる行との対応の等式が無い。"
                "「どこかに見える行が1つでもあれば通る」形になっている。\n" + sql
            )
        checked.append(name)

    assert not uncallable, (
        f"{bq.__name__} の次のメソッドを汎用引数で呼べず、ゲートを検査できなかった。"
        "引数の作り方（_dummy_for）を足すこと:\n  " + "\n  ".join(uncallable)
    )
    if bq.__name__ in _NO_VIEWER_GATED_READS:
        assert not checked, (
            f"{bq.__name__} に viewer ゲートの要る読み取りができた。"
            "_NO_VIEWER_GATED_READS から外し、ゲートを入れること"
        )
        return
    assert checked, f"{bq.__name__} に viewer を取るメソッドが1つも見つからない（検査が空振り）"
