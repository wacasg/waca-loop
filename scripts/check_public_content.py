"""公開前の内容チェック — 公開リポジトリに入れてはならないものを検出する.

対象は git が管理するファイル（追加予定の未追跡ファイルを含む）。見つかったら一覧を表示して
終了コード 1 を返す。CI・pre-push フック・テスト（tests/test_public_content.py）から呼ばれる。

検出するもの:
  - 入れてはならないファイル（org.toml・members.json・.env・鍵ファイル）
  - 秘密鍵の本文
  - 例示用ドメイン以外のメールアドレス
  - 実在のスプレッドシートIDや Cloud Run の URL らしき値
  - リポジトリ外の非公開文書への参照（「〇〇（非公開）」の形）
  - 開発の経緯（レビュー担当・指摘の重大度、経緯として書かれた日付）
  - ローカルの禁止語リスト（.public-denylist）に載っている語

.public-denylist は git の管理対象外のファイルで、組織名・人名・取引先名など、公開してはならない
固有の語を1行に1つ書く（# で始まる行と空行は無視、大文字・小文字は区別しない）。禁止語そのものを
リポジトリに入れないため、このファイルは各自の手元にだけ置く。別の場所に置く場合は環境変数
PUBLIC_DENYLIST にパスを指定する。

使い方:
    python scripts/check_public_content.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# このファイルとそのテストは検出パターンの例を含むため対象外にする。
SELF = {"scripts/check_public_content.py", "tests/test_public_content.py"}

FORBIDDEN_FILES = re.compile(
    r"(^|/)(org\.toml|members\.json|\.env|\.env\.(?!example$)[^/]+|\.public-denylist"
    r"|[^/]*\.pem|[^/]*\.p12|service-account[^/]*\.json|[^/]*\.key\.json)$"
)
# 記入例・テスト用の設定は対象外（実データではない）。
FORBIDDEN_FILE_EXCEPTIONS = {"sample/org.toml", "sample/members.json", "tests/org.test.toml"}

PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
# 例示用のドメインと、文書中で形式を示すためのサービスアカウントのドメイン。
ALLOWED_EMAIL_DOMAIN = re.compile(
    r"^(?:[a-z0-9-]+\.)*(?:example\.(?:org|com|net|invalid|jp)|change-me\.example)$"
    r"|^(?:users\.noreply\.github\.com|(?:[a-z0-9-]+\.)*gserviceaccount\.com)$",
    re.IGNORECASE,
)
# サービスアカウントのアドレスは、プロジェクトID の部分が変数・例示であれば可。
SERVICE_ACCOUNT = re.compile(r"[A-Za-z0-9._%+-]+@([a-z0-9-]+)\.iam\.gserviceaccount\.com")
# gcp-sa-* は Google が管理するサービスエージェントのドメインで、組織固有ではない。
ALLOWED_SA_PROJECT = re.compile(
    r"^(?:yyy|your-[a-z0-9-]+|[a-z0-9-]*example[a-z0-9-]*|gcp-sa-[a-z0-9-]+)$"
)

SPREADSHEET_ID = re.compile(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{25,})")
# Cloud Run の URL は、プロジェクト番号が例示（123456789012）か伏せ字の場合のみ可。
RUN_APP_URL = re.compile(r"https://[a-z0-9-]+-(\d{6,})\.[a-z0-9-]+\.run\.app")
RUN_APP_HASH_URL = re.compile(r"https://[a-z0-9-]+-([a-z0-9]{10})-[a-z]{2}\.a\.run\.app")
ALLOWED_PROJECT_NUMBERS = {"123456789012"}

NON_PUBLIC_DOC = re.compile(r"[^\s「『（(、。,]+[（(]非公開[）)]")

# 開発の経緯の書き込み。コメント・文書には現在の仕様と理由だけを書く（CONTRIBUTING.md）。
# レビュー担当・指摘の重大度・指摘番号の書き込み。.claude/agents/ はレビュー用エージェントの
# 定義そのものなので対象外。
ATTRIBUTION = re.compile(
    r"\b(?:Codex|red-team)\b|\b(?:HIGH|MED|LOW)-\d|(?:レビュー|監査)\s*[FHLM]-?\d"
)
ATTRIBUTION_EXEMPT_PREFIXES = (".claude/agents/",)
# 経緯としての日付（YYYY-MM または YYYY-MM-DD）。引用符の中（データ・例示）、時刻付きの
# タイムスタンプ、「例」「e.g.」の後ろは対象外。tests/ と sample/ はデータが多いため、
# コメント（# 以降）だけを見る。CHANGELOG.md はリリース日を書く場所なので対象外。
HISTORY_DATE = re.compile(r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])(?:-\d{2})?\b")
DATA_DIR_PREFIXES = ("tests/", "sample/")
DATE_EXEMPT_FILES = {"CHANGELOG.md"}
EXAMPLE_MARKER = re.compile(r"e\.g\.|例|for example|such as", re.IGNORECASE)


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return sorted({p for p in out.splitlines() if p and (ROOT / p).is_file()})


def load_denylist() -> list[str]:
    path = Path(os.environ.get("PUBLIC_DENYLIST", ROOT / ".public-denylist"))
    if not path.is_file():
        return []
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        word = line.strip()
        if word and not word.startswith("#"):
            words.append(word.casefold())
    return words


def _is_quoted(before: str) -> bool:
    if len(re.findall(r"`+", before)) % 2 == 1:
        return True
    return any(before.count(q) % 2 == 1 for q in ('"', "'"))


def history_date(path: str, text: str) -> str | None:
    """経緯として書かれた日付を返す（無ければ None）."""
    if path in DATE_EXEMPT_FILES:
        return None
    scope = text
    if path.startswith(DATA_DIR_PREFIXES):
        hash_at = text.find("#")
        if hash_at < 0 or _is_quoted(text[:hash_at]):
            return None
        scope = text[hash_at:]
    for match in HISTORY_DATE.finditer(scope):
        before, after = scope[: match.start()], scope[match.end():]
        if re.match(r"[T ]\d{2}:\d{2}", after) or re.match(r"/\d", after):
            continue
        if _is_quoted(before) or EXAMPLE_MARKER.search(before):
            continue
        return match.group(0)
    return None


def check_line(path: str, text: str, denylist: list[str]) -> list[str]:
    problems = []
    if PRIVATE_KEY.search(text):
        problems.append("秘密鍵の本文")
    for match in EMAIL.finditer(text):
        domain = match.group(1).rstrip(".")
        if ALLOWED_EMAIL_DOMAIN.match(domain):
            continue
        problems.append(f"例示用以外のメールアドレス: {match.group(0)}")
    for match in SERVICE_ACCOUNT.finditer(text):
        if not ALLOWED_SA_PROJECT.match(match.group(1)):
            problems.append(f"実在のプロジェクトらしきサービスアカウント: {match.group(0)}")
    for match in SPREADSHEET_ID.finditer(text):
        problems.append(f"スプレッドシートID: {match.group(1)[:8]}…")
    for match in RUN_APP_URL.finditer(text):
        if match.group(1) not in ALLOWED_PROJECT_NUMBERS:
            problems.append(f"Cloud Run の URL: {match.group(0)}")
    for match in RUN_APP_HASH_URL.finditer(text):
        if set(match.group(1)) != {"x"}:
            problems.append(f"Cloud Run の URL: {match.group(0)}")
    for match in NON_PUBLIC_DOC.finditer(text):
        problems.append(f"非公開文書への参照: {match.group(0)}")
    if not path.startswith(ATTRIBUTION_EXEMPT_PREFIXES):
        match = ATTRIBUTION.search(text)
        if match:
            problems.append(f"レビュー・指摘の経緯: {match.group(0)}")
    date = history_date(path, text)
    if date:
        problems.append(f"経緯としての日付: {date}")
    folded = text.casefold()
    for word in denylist:
        if word in folded:
            problems.append("禁止語リストの語を含む")
            break
    return problems


def find_problems() -> list[str]:
    denylist = load_denylist()
    found = []
    for path in tracked_files():
        if FORBIDDEN_FILES.search(path) and path not in FORBIDDEN_FILE_EXCEPTIONS:
            found.append(f"{path}: 公開リポジトリに入れてはならないファイル")
            continue
        if path in SELF:
            continue
        try:
            text = (ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for problem in check_line(path, line, denylist):
                found.append(f"{path}:{lineno}: {problem}")
    return found


def main() -> int:
    problems = find_problems()
    denylist_note = "あり" if load_denylist() else "なし（.public-denylist が見つからない）"
    if problems:
        print("公開してはならない内容が見つかった:")
        for problem in problems:
            print(f"  {problem}")
        print(f"\n{len(problems)} 件。禁止語リスト: {denylist_note}")
        return 1
    print(f"問題なし。禁止語リスト: {denylist_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
