#!/usr/bin/env bash
# G3 — DDL/移行クエリ編集の承認リマインダ（PreToolUse / Edit・Write）。
# 編集自体は止めない（適用＝bq 実行は G1 が止める）。警告のみ。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"

INPUT="$(hook_input)"
FP="$(json_get "$INPUT" "tool_input.file_path")"
[ -n "$FP" ] || exit 0
case "$FP" in
  *sql/ddl/*|*sql/migrations/*)
    notify "[WACA Loop G3] DDL/移行クエリの編集です。適用は「提示 → 管理者の承認 → 実行」の順で行う。編集は止めません。" ;;
esac
exit 0
