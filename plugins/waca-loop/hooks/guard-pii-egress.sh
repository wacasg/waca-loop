#!/usr/bin/env bash
# G5 — 個票生テキスト流出ガード（PreToolUse / 外部送信系ツール）。warn（block はしない）。
#
# 検出器は **再宣言しない**（仕様書 G5: 正規表現の二重管理はドリフトの元）。
# waca_loop.ingest.anonymize の公開関数 detectors() を import して使い、
# import できない環境（WACA Loop リポ外）では FLOOR-01 の原則リマインドだけを出す。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"

INPUT="$(hook_input)"
TOOL="$(json_get "$INPUT" "tool_name")"
PAYLOAD="$(json_get "$INPUT" "tool_input")"
[ -n "$PAYLOAD" ] || exit 0

# 検査は**別ファイルのスクリプトに stdin で渡す**。payload を argv に置くと、その内容が
# `ps` で他ユーザーから読める — この hook の payload は構造上いちばん氏名・連絡先を含んで
# いる可能性が高いもので、FLOOR-01 そのものである。
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
FOUND=""
STATUS="skipped"
if [ -n "$ROOT" ] && [ -f "$ROOT/src/waca_loop/ingest/anonymize.py" ]; then
  if FOUND="$(printf '%s' "$PAYLOAD" | PYTHONPATH="$ROOT/src" python3 "$DIR/_pii_scan.py" 2>/dev/null)"; then
    STATUS="ran"
  else
    STATUS="failed"
    FOUND=""
  fi
fi

if [ "$STATUS" = "failed" ]; then
  notify "[WACA Loop G5 / FLOOR-01] 外部送信（${TOOL}）です。**検出器を実行できませんでした**（検査は行われていません）。個票の生テキスト・氏名・連絡先を含めていないか、目視で確認してください。"
elif [ -n "$FOUND" ]; then
  notify "[WACA Loop G5 / FLOOR-01] 外部送信（${TOOL}）の内容に個人情報らしき値を検出しました: ${FOUND}
生の値を外へ出す前に、必ず利用目的を確認し、匿名化仕様の承認を得てください。
get_pii_anonymization_guidance は**列名のみ**で呼ぶこと（実際の値を引用・送信しない）。"
else
  notify "[WACA Loop G5 / FLOOR-01] 外部送信ツール（${TOOL}）です。個票の生テキスト・氏名・連絡先を含めていないか確認してください。"
fi
exit 0
