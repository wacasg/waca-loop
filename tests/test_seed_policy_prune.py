"""正典から消えたポリシー行が、本番からも消えることを固定する（W3・SQL 契約）.

**row_id 単位の upsert** だけでは、正典（シート）から消えた行を消せない。消えた行が
残ると、正典に無い行が正典の行と**並んで配達される**。**同期の入口だけ作っても、出口が消せなければ正典と一致しない。**

`--prune` がそれを塞ぐ（`tests/test_sync_policy_sheet.py` はシート→行データの変換までで、
投入経路には触れていない）。ここは投入側の DELETE の向きだけを
見る — naive 版（`row_id IN UNNEST(@ids)` のまま、つまり入れ替える行だけ消す）は、
既存のテストを1つも落とさずに欠番の行を本番へ残す。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

_SEED = [
    {"row_id": "KYO-1", "scope_unit": "kyozai", "line": "新しい行", "level": 1},
]


class _Job:
    num_dml_affected_rows = 3

    def result(self):
        return iter(())


class _Client:
    def __init__(self):
        self.queries: list[tuple[str, list]] = []
        self.loaded: list[dict] = []

    def query(self, sql, job_config=None):
        params = getattr(job_config, "query_parameters", None) or []
        self.queries.append((sql, [p.values if hasattr(p, "values") else p.value for p in params]))
        return _Job()

    def get_table(self, name):
        class _T:
            schema = ()
        return _T()

    def load_table_from_json(self, rows, table, job_config=None):
        self.loaded.extend(rows)
        return _Job()


@pytest.fixture
def seed_file(tmp_path):
    f = tmp_path / "seed.json"
    f.write_text(json.dumps(_SEED, ensure_ascii=False), encoding="utf-8")
    return str(f)


def _run(monkeypatch, seed_file, *flags):
    import seed_policy  # noqa: PLC0415

    class _Settings:
        gcp_project = "p"
        policy_table = "p.d.policy"

    client = _Client()
    monkeypatch.setattr("waca_loop.config.get_settings", lambda: _Settings())
    monkeypatch.setattr("waca_loop.bq.make_client", lambda settings: client)
    monkeypatch.setattr(sys, "argv", ["seed_policy.py", seed_file, *flags])
    assert seed_policy.main() == 0
    return client


def test_prune_deletes_every_row_the_canon_no_longer_has(monkeypatch, seed_file):
    client = _run(monkeypatch, seed_file, "--prune")
    deletes = [(sql, prm) for sql, prm in client.queries if "DELETE" in sql]
    assert any("NOT IN UNNEST(@ids)" in sql for sql, _ in deletes), \
        "正典に無い行を消していない（シートに無い行が正典の行と並んで配達される）"
    assert all(prm == [["KYO-1"]] for _, prm in deletes), \
        "消す/残す判断が seed の row_id 以外に依存している"
    assert [r["row_id"] for r in client.loaded] == ["KYO-1"]


def test_without_prune_only_the_replaced_rows_are_deleted(monkeypatch, seed_file):
    """既定は upsert。`--prune` は明示して使う — 消す操作を既定にしない。"""
    client = _run(monkeypatch, seed_file)
    deletes = [sql for sql, _ in client.queries if "DELETE" in sql]
    assert deletes and not any("NOT IN UNNEST" in sql for sql in deletes)


def test_a_dry_run_writes_nothing_and_says_it_would_prune(monkeypatch, seed_file, capsys):
    client = _run(monkeypatch, seed_file, "--dry-run", "--prune")
    assert client.queries == [] and client.loaded == []
    assert "DELETE" in capsys.readouterr().out
