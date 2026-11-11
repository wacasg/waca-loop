"""取り下げ行の除外を「規約」から「テスト」へ.

取り下げは追記方式なので、取り下げた行は `usage_log` の中で**生きた行とバイト単位で同じ**で
ある。行そのものは何も語らないので、隠れているかどうかは**読み手が anti-join したか**
だけで決まる。集計スクリプトや照会を1つ足すたびに取りこぼしの余地が生まれ、人が数え直す
方法では漏れを防げない。そこで、リポジトリ内の `usage_log` を読む SQL を機械的に数える。
さらに Lite のような SQL を書かない backend は、store 実装の名簿と公開メソッドの振る舞いで守る。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fake_bq import FakeBigQueryClient
from waca_loop.ledger import usage_log
from waca_loop.ledger.usage_log import BigQueryUsageLogStore, InMemoryUsageLogStore
from waca_loop.models import UsageRecord, UseCategory

ROOT = Path(__file__).resolve().parents[1]
SEARCH = ("src", "scripts", "docs", "sql")

#: 除外が要らないと**判断した**もの。増やすときは理由を書くこと。
ALLOWED = {
    # 表を定義する側。ここに anti-join は書けない。
    "sql/ddl/03_usage_log.sql",
    "sql/ddl/14_usage_withdrawals.sql",
    # このテスト自身と、設計を説明している文書。
    "tests/test_withdrawal_is_excluded_everywhere.py",
}

#: 大文字の ``FROM`` だけを見る — 小文字は Python の ``from … import`` で、SQL ではない。
#: この区別が要るのは、モジュール名がテーブル名と同じだから。
#: 除外が書かれている証拠。テーブル名が直接書かれているとは限らない — 本番のクエリは
#: f-string で差し込むので、そこに現れるのは差し込み側の名前になる。
_EXCLUSION_MARKERS = ("usage_withdrawals", "_not_withdrawn", "{withdrawals}")

#: 除外を**書かないと決めた**一文に貼る印（再送の重複排除）。
#: ファイル単位の :data:`ALLOWED` では、その中にある本物の読み手まで一緒に免除してしまう
#: ので、文単位の逃げ道をひとつだけ名前付きで用意する。数は下のテストで固定する — 逃げ道が
#: 増えていないことを機械が数えるなら、逃げ道があること自体は隠れない。
_WAIVER = "withdrawal-exclusion-waived"

#: 逃げ道を使っていい一文の名簿（相対パス → 件数）。増やすときは理由を書くこと。
WAIVED = {"src/waca_loop/ledger/usage_log.py": 1}

#: 逃げ道が貼られていてよい**関数**と、その照会が返してよい列。件数だけを固定すると、印を
#: 同じファイル内の**内容を返す**照会へ移しても名簿は緑のまま。
#: 「log_id しか返さないから除外が要らない」が免除の理由なので、そこまで機械で押さえる。
WAIVED_STATEMENTS = {
    ("waca_loop.ledger.usage_log", "BigQueryUsageLogStore.existing_log_id"): ("log_id",),
}

#: 引用符は1文字とは限らない。手順書は Markdown の中の shell の中の SQL なので
#: バッククォートがエスケープされて ``\\`` として現れる。これを見落とすと、
#: **本来この番人が守るはずの `MANUAL_users.md` だけが検査対象から外れる**。
_USAGE_LOG_SQL = re.compile(r"FROM\s+[\\`'\"]*[\w.\-{}]*usage_log")


def _files(root: Path, search: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for d in search:
        out += [p for p in (root / d).rglob("*") if p.suffix in {".py", ".sql", ".md"}]
    return out


def _offenders(root: Path = ROOT, search: tuple[str, ...] = SEARCH) -> list[tuple[str, int, str]]:
    bad = []
    for path in _files(root, search):
        rel = str(path.relative_to(root))
        if rel in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in _USAGE_LOG_SQL.finditer(text):
            # **同じ文の中**を見る。前後に広く取ると、すぐ下の説明文が表名に触れているだけで
            # 満足してしまい、肝心の照会から除外が抜けても気づかない。SQL 文は空行で切れるので、
            # そこまで。
            rest = text[m.end():]
            end = rest.find("\n\n")
            statement = rest if end < 0 else rest[:end]
            if any(marker in statement for marker in _EXCLUSION_MARKERS):
                continue
            if _WAIVER in statement:
                continue
            line = text.count("\n", 0, m.start()) + 1
            bad.append((rel, line, text[m.start():m.start() + 60].replace("\n", " ")))
    return bad


def test_every_sql_read_of_the_ledger_excludes_withdrawn_rows():
    """SQL を書く読み手向けの番人。手順書・提案・スクリプトの取り下げ漏れを拾う.

    この grep 版は `docs/*.md` や運用 SQL も見る。SQL を書かない backend は下の
    store-behavior roster が守るので、片方に寄せず併存させる。
    """
    bad = _offenders()
    assert not bad, "取り下げ行を除外していない usage_log の読み手:\n" + "\n".join(
        f"  {f}:{ln}  {snippet}" for f, ln, snippet in bad
    )


def test_the_waiver_is_used_exactly_where_the_roster_says():
    """逃げ道（``_WAIVER``）の数を固定する.

    :data:`WAIVED` に無い場所で印が使われたら赤。件数が合わなくても赤 — 「1件だけのはず」が
    2件になったときに気づけないと、この印は除外の免罪符になる。
    """
    # 数えるのは**照会を書く場所**だけ。`docs/` は免除を説明する場所で、そこに印の名前が
    # 出てくるのは記録として正しい。
    found: dict[str, int] = {}
    for path in _files(ROOT, ("src", "scripts", "sql")):
        rel = str(path.relative_to(ROOT))
        n = path.read_text(encoding="utf-8", errors="ignore").count(_WAIVER)
        if n:
            found[rel] = n
    assert found == WAIVED, f"取り下げ除外の免除が名簿と違う: {found} != {WAIVED}"


def test_the_waived_statement_still_returns_nothing_but_an_id():
    """免除された照会が、名簿の列しか返さないこと（印を移して使い回せないようにする）。"""
    import importlib

    for (module_name, dotted), columns in WAIVED_STATEMENTS.items():
        module = importlib.import_module(module_name)
        target: Any = module
        for part in dotted.split("."):
            target = getattr(target, part)
        src = inspect.getsource(target)
        assert src.count(_WAIVER) == 1, f"{dotted}: 印が無い（名簿と実装がずれている）"
        # 行頭の SELECT だけ（説明文の中の "A SELECT before the insert" を拾わない）
        selects = re.findall(r"^\s*SELECT\s+(.+?)\s*$", src, re.MULTILINE)
        assert selects, f"{dotted}: SELECT が見つからない"
        for select in selects:
            got = tuple(c.strip() for c in select.split(","))
            assert got == columns, f"{dotted}: 免除された照会が {got} を返している"


def test_the_check_actually_catches_a_missing_exclusion(tmp_path):
    """この番人が本当に噛むこと（飾りのテストにしない）."""
    fake = tmp_path / "src"
    fake.mkdir()
    (fake / "leaky.py").write_text(
        'SQL = "SELECT summary FROM `p.d.usage_log` WHERE 1=1"\n', encoding="utf-8"
    )
    assert _offenders(tmp_path, ("src",)), "除外の無い読み手を見逃した"
    (fake / "leaky.py").write_text(
        'SQL = "SELECT summary FROM `p.d.usage_log` WHERE log_id NOT IN '
        '(SELECT log_id FROM `p.d.usage_withdrawals`)"\n',
        encoding="utf-8",
    )
    assert not _offenders(tmp_path, ("src",)), "除外が書いてあるのに誤検知した"


@pytest.mark.parametrize("path", sorted(ALLOWED))
def test_the_allowlist_has_no_stale_entries(path):
    """許した覚えのないファイルが残らないように — 消えたら気づく."""
    assert (ROOT / path).exists()


# --- SQL を書かない backend も守る、store 公開メソッドの振る舞い番人 ---------------

EXPECTED_USAGE_LOG_STORES = frozenset({"BigQueryUsageLogStore", "InMemoryUsageLogStore"})
PUBLIC_USAGE_LOG_METHODS = frozenset({
    "delete",
    "distinct_actors",
    "get_withdrawn",
    "list_withdrawn",
    "record",
    "set_status",
    "withdraw",
})


@dataclass(frozen=True)
class UsageLogStoreProbe:
    cls: type
    factory: Callable[[], Any]


class _RowsJob:
    def __init__(self, rows: list[dict], affected: int = 0) -> None:
        self._rows = rows
        self.num_dml_affected_rows = affected

    def result(self):
        return iter(self._rows)


def _params(job_config: Any) -> dict[str, Any]:
    return {
        p.name: getattr(p, "values", None) if hasattr(p, "values") else p.value
        for p in getattr(job_config, "query_parameters", []) or []
    }


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


class _UsageLogFakeBigQueryClient(FakeBigQueryClient):
    """FakeBigQueryClient plus the array-driven lookup used by R6's two-stage read."""

    def query(self, sql: str, job_config: Any = None) -> _RowsJob:
        if "FROM UNNEST(@log_ids)" not in sql:
            return super().query(sql, job_config)
        params = _params(job_config)
        wanted = set(params["log_ids"])
        start = _as_datetime(params["part_start"])
        end = _as_datetime(params["part_end"])
        cur = self._conn.execute("SELECT * FROM p__d__usage_log")
        rows = []
        for row in cur.fetchall():
            got = dict(row)
            occurred_at = _as_datetime(got["occurred_at"])
            if got["log_id"] in wanted and start <= occurred_at < end:
                rows.append(got)
        return _RowsJob(rows)


class _UsageLogSettings:
    usage_log_table = "p.d.usage_log"
    usage_withdrawals_table = "p.d.usage_withdrawals"
    usage_log_retention_days = 730


def _withdrawal_seed() -> dict:
    return {
        "withdrawal_id": "seed",
        "log_id": "seed",
        "actor_id": "p_seed",
        "actor_raw": None,
        "reason": "private",
        "withdrawn_at": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _bigquery_usage_store() -> BigQueryUsageLogStore:
    client = _UsageLogFakeBigQueryClient({
        _UsageLogSettings.usage_withdrawals_table: [_withdrawal_seed()],
    })
    return BigQueryUsageLogStore(client, _UsageLogSettings())


USAGE_LOG_STORE_PROBES = {
    "BigQueryUsageLogStore": UsageLogStoreProbe(BigQueryUsageLogStore, _bigquery_usage_store),
    "InMemoryUsageLogStore": UsageLogStoreProbe(InMemoryUsageLogStore, InMemoryUsageLogStore),
}


def _live_usage_log_stores() -> set[str]:
    return {
        name
        for name, obj in inspect.getmembers(usage_log, inspect.isclass)
        if obj.__module__ == usage_log.__name__ and name.endswith("UsageLogStore")
    }


def _usage_record(actor_id: str, *, status: str = "recorded") -> UsageRecord:
    return UsageRecord(
        actor_id=actor_id,
        actor_raw=None,
        client="guardian",
        data_kinds=["internal"],
        ingested_via="mcp",
        status=status,
        summary=f"summary:{actor_id}",
        use_category=UseCategory.L1,
    )


def _record(store: Any, actor_id: str, *, status: str = "recorded") -> str:
    return store.record(
        _usage_record(actor_id, status=status),
        occurred_at=datetime.now(UTC),
    )


def test_usage_log_store_behavior_roster_matches_live_implementations():
    """生きている台帳 store 実装は、名簿に載るまで取り下げ契約を緑にしない."""
    assert _live_usage_log_stores() == set(USAGE_LOG_STORE_PROBES) == EXPECTED_USAGE_LOG_STORES


@pytest.mark.parametrize("name", sorted(USAGE_LOG_STORE_PROBES))
def test_usage_log_store_probe_covers_the_public_ledger_surface(name: str):
    """名簿にある実装が、R6 で守るべき公開メソッドをすべて持つこと."""
    probe = USAGE_LOG_STORE_PROBES[name]
    public = {n for n in dir(probe.cls) if not n.startswith("_")}
    assert PUBLIC_USAGE_LOG_METHODS <= public


@pytest.mark.parametrize("name", sorted(USAGE_LOG_STORE_PROBES))
def test_withdrawn_rows_are_hidden_by_every_usage_log_store(name: str):
    """SQL の有無に関係なく、通常の台帳経路は取り下げ行を返さず数えない."""
    store = USAGE_LOG_STORE_PROBES[name].factory()

    withdrawn_id = _record(store, "p_owner", status="pending")
    kept_id = _record(store, "p_kept", status="pending")
    assert store.distinct_actors(3650) == {"p_owner", "p_kept"}

    assert store.withdraw(
        withdrawn_id, actor_id="p_owner", actor_raw=None, reason="private"
    ) is True
    assert store.withdraw(
        withdrawn_id, actor_id="p_owner", actor_raw=None, reason="private"
    ) is False
    assert store.withdraw(
        kept_id, actor_id="p_owner", actor_raw=None, reason="private"
    ) is False

    assert store.distinct_actors(3650) == {"p_kept"}
    assert store.set_status(withdrawn_id, "recorded") is False
    assert store.delete(withdrawn_id) is False
    assert store.set_status(kept_id, "recorded") is True

    listed = store.list_withdrawn(10)
    assert [r["log_id"] for r in listed] == [withdrawn_id]
    assert "summary" not in listed[0]
    assert "reason" not in listed[0]

    detail = store.get_withdrawn(withdrawn_id)
    assert detail and detail["summary"] == "summary:p_owner"
    assert detail["reason"] == "private"
    assert store.get_withdrawn(kept_id) is None
    assert store.delete(kept_id) is True
