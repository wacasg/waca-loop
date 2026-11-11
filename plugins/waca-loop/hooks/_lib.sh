#!/usr/bin/env bash
# hook 共通ヘルパ。stdin の hook JSON から値を取り出す（jq があれば jq、無ければ python3）。
set -euo pipefail

hook_input() { cat; }

json_get() {
  # json_get <json> <dotted.path>  — 見つからなければ空文字
  local json="$1" path="$2"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$json" | jq -r --arg p "$path" '
      def get($p): reduce ($p | split(".")[]) as $k (.; if type=="object" then .[$k] else null end);
      get($p) // "" | if type=="string" then . else tojson end' 2>/dev/null || true
  else
    printf '%s' "$json" | python3 -c '
import json,sys
path=sys.argv[1].split(".")
try: cur=json.load(sys.stdin)
except Exception: sys.exit(0)
for k in path:
    if isinstance(cur,dict) and k in cur: cur=cur[k]
    else: sys.exit(0)
print(cur if isinstance(cur,str) else json.dumps(cur,ensure_ascii=False))
' "$path" 2>/dev/null || true
  fi
}

# waca-loop リポジトリ内でのみ効かせたいガード用。
in_waca_loop_repo() {
  local root
  root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$root" ] && [ -f "$root/src/waca_loop/server.py" ]
}

deny() { echo "$*" >&2; exit 2; }   # PreToolUse: exit 2 = ブロック（stderr がモデルへ渡る）

# 通知のみ（ブロックしない）。**素の stdout は使わないこと。**
#
# Claude Code の hook の仕様:
# exit 0 の素の stdout がモデルのコンテキストへ入るのは UserPromptSubmit /
# UserPromptExpansion / SessionStart の3つだけ。それ以外のイベント（Stop・PreToolUse を
# 含む）では **デバッグログに書かれるだけで、モデルにもトランスクリプトにも出ない**。
# 素の stdout で書くと、G2/G3/G4/G5 の警告は1行も届かない。
#
# 代わりに JSON の `systemMessage` で返す。これは「ユーザに表示され、かつモデルの
# コンテキストへ system message として入る」。exit 2（ブロック）とは違い会話を止めない
# ので、偽陽性のある warn ゲートにはこちらが正しい。
notify() {
  printf '%s' "$*" | python3 -c 'import json,sys; print(json.dumps({"systemMessage": sys.stdin.read().rstrip("\n")}, ensure_ascii=False))'
  exit 0
}
