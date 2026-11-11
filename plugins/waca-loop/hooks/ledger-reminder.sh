#!/usr/bin/env bash
# UserPromptSubmit — 台帳記録の督促 (SPEC_ops §6 規約1)。
#
# なぜ必要か:
#   規約は SessionStart 時にコンテキストへ入るが、長いセッションでは背景情報として薄れる。
#   長時間・多数の往復で組織の知識を読み続けても、記録が残らないことがある。一度言うだけでは
#   足りない。
#
# これは**強制ではなく督促**である（hook からツール呼び出しは代行できない）。
# 実際に守ったかはサーバ側 R3b（retrieval あり・usage_recorded ゼロ = silent）が検出する。
#
# プラグイン版: waca-loop リポの外でも効く。組織の業務かどうかの判定は
# hook では行わない — 判定はモデルとユーザーの領分で、機械が誤判定すると
# 「他組織の会話を組織の台帳に書く」最悪の事故（SPEC_ops §1-4）につながる。
set -euo pipefail

# 二重表示の回避: そのリポジトリが .claude/settings.json に同じ督促 hook を登録しているなら黙る
# （プラグインとリポジトリの両方に登録した場合に、督促が2回出ないようにするため）。
root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$root" ] && grep -qs 'ledger-reminder.sh' "$root/.claude/settings.json"; then
  exit 0
fi

cat <<'MSG'
[WACA Loop 運用ルール — 毎ターン]
このターンが組織の業務なら、応答の前に get_applicable_policy、応答の後に log_usage を
呼ぶこと（要約のみ・逐語は送らない）。組織の業務と無関係（他組織・副業・私事）なら
書き込みは一切せず、帰属が曖昧なら記録してよいかを先にユーザへ確認すること。
MSG
