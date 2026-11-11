"""公開前の内容チェック（scripts/check_public_content.py）をテストとして実行する.

公開リポジトリに入れてはならないもの（実在のメールアドレス・鍵・非公開文書への参照など）が
入ると、ここで失敗する。手元に .public-denylist があれば、その禁止語も検査される。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_public_content.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_public_content", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_has_no_content_that_must_not_be_public():
    problems = _load().find_problems()
    assert problems == [], "\n".join(problems)


def test_the_checker_catches_what_it_claims_to():
    check = _load()
    assert check.check_line("docs/a.md", "連絡は taro@realcorp.co.jp まで", [])
    assert check.check_line("docs/a.md", "foo@gmail.com", [])
    assert check.check_line("tests/a.py", "someone@gmail.com", [])
    assert check.check_line("docs/a.md", "社内メモ（非公開） を参照", [])
    assert check.check_line("docs/a.md", "-----BEGIN PRIVATE KEY-----", [])
    assert check.check_line(
        "docs/a.md", "https://waca-loop-serve-987654321098.asia-northeast1.run.app", [])
    assert check.check_line("docs/a.md", "取引先の件", ["取引先"])
    assert check.FORBIDDEN_FILES.search("org.toml")
    assert check.FORBIDDEN_FILES.search(".env.setup")
    assert not check.FORBIDDEN_FILES.search(".env.example")


def test_the_checker_catches_development_history():
    check = _load()
    assert check.check_line("src/a.py", "# 担当者の指摘で直した (Codex)", [])
    assert check.check_line("src/a.py", "# 漏えいの穴を塞ぐ (HIGH-2)", [])
    assert check.check_line("docs/a.md", "レビュー F9 で追加した", [])
    assert check.check_line("src/a.py", "# 2025-01-15 に挙動を変えた", [])
    assert check.check_line("docs/a.md", "決定（2025-01 管理者）", [])
    assert check.check_line("tests/a.py", 'x = 1  # 2025-01-15 の回帰', [])


def test_the_checker_leaves_data_and_examples_alone():
    check = _load()
    assert not check.check_line("docs/a.md", "user@example.org", [])
    assert not check.check_line("tests/a.py", "someone@mail.example.com", [])
    assert not check.check_line("tests/a.py", 'row = {"at": "2025-01-15"}', [])
    assert not check.check_line("tests/a.py", 'ts = "2025-01-15T09:00:00Z"', [])
    assert not check.check_line("src/a.py", "period: str  # survey period, e.g. 2025-06", [])
    assert not check.check_line("src/a.py", "# 期間は ``2025-06`` のように書く", [])
    assert not check.check_line("CHANGELOG.md", "## [1.0.0] - 2025-01-15", [])
    assert not check.check_line("tests/a.py", '"client": "codex-cli",', [])
