#!/usr/bin/env bash
# G2（三色語彙ハードコードゲート）+ G4（レビューゲート未実施の検知） — Stop フック。
# どちらも **warn**。偽陽性があるため block にはしない（docs/HOOKS_SPEC.md）。
# waca-loop リポジトリ以外では何もしない。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
cat >/dev/null   # stdin を捨てる

in_waca_loop_repo || exit 0
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

DIFF_FILES="$(git diff --name-only HEAD -- src sql 2>/dev/null; git diff --cached --name-only -- src sql 2>/dev/null)"
DIFF_FILES="$(printf '%s\n' "$DIFF_FILES" | sort -u | sed '/^$/d')"
[ -n "$DIFF_FILES" ] || exit 0

MSG=""

# --- G2: 三色語彙・tier 直書き比較の混入 -----------------------------------
# 定義元（`vocab-definition-site` マーカーを持つファイル）は対象外。
HITS=""
while IFS= read -r f; do
  [ -f "$f" ] || continue
  grep -q 'vocab-definition-site' "$f" && continue
  added="$(git diff HEAD -- "$f" | grep '^+' | grep -v '^+++' || true)"
  [ -n "$added" ] || continue
  h="$(printf '%s\n' "$added" | grep -nE '"(red|yellow|green)"|'"'"'(red|yellow|green)'"'"'|🔴|🟡|🟢|[赤黄緑]色|==\s*"(personal|team|dept|org)"' || true)"
  [ -n "$h" ] && HITS="${HITS}
  ${f}:
$(printf '%s\n' "$h" | sed 's/^/    /')"
done <<< "$DIFF_FILES"

if [ -n "$HITS" ]; then
  MSG="${MSG}[WACA Loop G2] 三色語彙または tier 文字列の直書き比較が差分に入っています。
採用レベルは level 0/1/2 + is_floor、可視性判定は retrieval/acl.py の関数経由が正典です。
定義元のファイルには 'vocab-definition-site' マーカーを付けると除外されます。${HITS}

"
fi

# --- G4: レビューゲート（spec-guardian ＋ acl-privacy-auditor）の実施痕跡 ---
STAMP=".claude/review-stamp"
if [ ! -f "$STAMP" ] || [ "$(find "$STAMP" -newermt '-6 hours' 2>/dev/null)" = "" ]; then
  MSG="${MSG}[WACA Loop G4] src/ または sql/ に差分がありますが、このセッションでレビュー
（spec-guardian ＋ acl-privacy-auditor）を通した痕跡がありません。
コミット前に両方を通し、実施したら \`touch .claude/review-stamp\` を実行してください。"
fi

# 届け方は notify()（JSON systemMessage）。素の stdout は Stop では捨てられる — _lib.sh 参照。
[ -n "$MSG" ] && notify "$MSG"
exit 0
