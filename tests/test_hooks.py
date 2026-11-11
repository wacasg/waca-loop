"""WACA Loop のガード hook の挙動を固定する (docs/HOOKS_SPEC.md, plugins/waca-loop).

hook はシェルスクリプトなので、テストを書かないと挙動が固定されない。たとえば本番へ UPDATE を
発行する `python scripts/seed_sfaq.py` はコマンド列に `bq` の字が無く、`bq` だけを見るガード
は素通りする。**止めるものを名乗る以上、止まることをテストで示す。**

exit 2 = block（stderr がモデルへ渡る）、exit 0 = 通す。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "plugins" / "waca-loop" / "hooks"


def _run(script: str, payload: dict, env: dict | None = None) -> subprocess.CompletedProcess:
    import os

    return subprocess.run(
        ["bash", str(HOOKS / script)],
        input=json.dumps(payload), text=True, capture_output=True,
        env={**os.environ, **(env or {})}, cwd=str(HOOKS),
    )


def _bq(cmd: str, env: dict | None = None) -> int:
    return _run("guard-bq-write.sh", {"tool_input": {"command": cmd}}, env).returncode


@pytest.mark.parametrize("cmd", [
    "bq load waca_loop_ledger.usage_log rows.json",
    "bq query --use_legacy_sql=false 'DELETE FROM `p.waca_loop_knowledge.questions`'",
    "bq rm -t p:waca_loop_grounding.terms",
    # bq を経由しない経路
    "PYTHONPATH=src python scripts/seed_sfaq.py --backfill-embeddings --regenerate",
    "python scripts/seed_policy.py sample/policy.json",
    # waca_loop_integration の字を混ぜて別データセットへ書く偽装
    "bq load waca_loop_integration.x a.json && bq load waca_loop_ledger.usage_log b.json",
    # --dry-run の字を混ぜて本番書込を通す迂回
    "echo --dry-run; bq load waca_loop_ledger.usage_log rows.json",
    "python scripts/seed_sfaq.py --regenerate --note='not a --dry-run'",
    # 呼び方を変えた迂回
    "python -m scripts.seed_sfaq --backfill-embeddings --regenerate",
    "python3 -m scripts.seed_terms sample/terms.json",
    'python3 -c "from google.cloud import bigquery; bigquery.Client().query(q)"',
    # 既定が dry-run のスクリプトは --apply* を付けたときだけ書く
    'python scripts/grant_role_unit.py --unit owner --person p_x --reason "r" --apply',
    "python scripts/purge_frozen_ern.py --apply-delete",
    "python scripts/register_members.py members.json --apply",
])
def test_production_writes_are_blocked(cmd):
    assert _bq(cmd) == 2, f"止まっていない: {cmd}"


@pytest.mark.parametrize("cmd", [
    "bq query --use_legacy_sql=false 'SELECT COUNT(*) FROM `p.d.questions`'",   # 読みは通す
    "bq load waca_loop_integration.source_items rows.json",  # 唯一の置き場
    "bq query --dry-run 'DELETE FROM x'",                                       # 提示は通す
    "python scripts/seed_sfaq.py --backfill-embeddings --dry-run",
    "git status",
    # 実行ではなく参照。ここを止めるとガードごと切られる
    "grep -n embed scripts/seed_sfaq.py",
    "git add scripts/seed_sfaq.py",
    "python -m pytest -q",          # -m だからといって止めない
    'python -c "print(1)"',         # BigQuery を触らない -c は止めない
    # --apply を付けなければ確認だけ
    "python scripts/grant_role_unit.py --unit owner --person p_x",
    "python scripts/purge_frozen_ern.py",
    "python scripts/register_members.py members.json",
])
def test_reads_and_dry_runs_pass(cmd):
    assert _bq(cmd) == 0, f"止めてはいけないものを止めた: {cmd}"


def test_approval_lets_the_write_through():
    """承認は環境変数で明示する（hook からは承認を観測できないため）."""
    cmd = "bq load waca_loop_ledger.usage_log rows.json"
    assert _bq(cmd) == 2
    assert _bq(cmd, {"WACA_LOOP_ALLOW_BQ_WRITE": "1"}) == 0


def test_the_pii_hook_never_echoes_the_matched_values():
    """種別名は出す、値は出さない（FLOOR-01）。payload を argv に置かないこと."""
    payload = {"tool_name": "mcp__gmail__send_message",
               "tool_input": {"body": "連絡先は taro@example.com です"}}
    out = _run("guard-pii-egress.sh", payload)
    assert out.returncode == 0            # 警告であって block ではない
    # 「検出できた」ことを積極的に確かめる — 値が出ていないだけの表明では、検出器が
    # 黙って動かなくても緑になる
    assert "email" in out.stdout, "検出器が動いていない（種別が出ていない）"
    assert "taro@example.com" not in out.stdout, "検出した値そのものを出力している"

    # payload は stdin で渡すこと（argv に置くと `ps` から他ユーザーに読める）。
    # 「python3 の行に $PAYLOAD が無い」という表明は書き方に依存して脆いので、
    # 検出器を呼ぶ行そのものを見る。
    source = (HOOKS / "guard-pii-egress.sh").read_text()
    invocation = next(ln for ln in source.splitlines() if "_pii_scan.py" in ln)
    assert "| PYTHONPATH" in invocation or invocation.strip().startswith("if FOUND"), invocation
    assert '_pii_scan.py "$PAYLOAD"' not in source, \
        "payload を argv に載せている（ps から読める — FLOOR-01）"
