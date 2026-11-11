#!/usr/bin/env bash
# G1 — 本番BQ書込ガード（PreToolUse / Bash）【最優先】
#
# このフックは、管理者の承認なしに既存データセットへ書き込むコマンドを止める。
# 新しいテーブルの試作は waca_loop_integration で行う。承認は hook からは観測できないので、
# **既定は block・承認は環境変数で明示**という形にする。
#
# フェイルクローズ: 判定できない bq 書込系は block に倒す（仕様書 G1）。
# --dry-run を含むコマンドは常に許可（既定の作業手順が dry-run 先行のため）。
#
# **`bq` コマンドだけを見ていては足りない**。たとえば `python scripts/seed_sfaq.py` による
# 再埋め込みは本番へ UPDATE を発行するが、コマンド列に `bq` の字は出てこない。
# リポジトリ内の BigQuery 書込スクリプトも名指しで止める。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"

INPUT="$(hook_input)"
CMD="$(json_get "$INPUT" "tool_input.command")"
[ -n "$CMD" ] || exit 0

# 承認済みの明示
[ "${WACA_LOOP_ALLOW_BQ_WRITE:-}" = "1" ] && exit 0

# dry-run は許可。ただし **コマンドが1本のときだけ**。
# 「文字列のどこかに --dry-run があれば通す」と、`echo --dry-run; bq load <本番> rows.json`
# で丸ごと抜けられる。
# 連結・パイプ・コマンド置換があるなら判定を諦め、
# フェイルクローズ側（＝下の通常判定）へ落とす。
if ! printf '%s' "$CMD" | grep -qE '[;&|]|\$\(|`'; then
  printf '%s' "$CMD" | grep -qE -- '(^|[[:space:]])--dry[-_]run([[:space:]]|=|$)' && exit 0
fi

lower="$(printf '%s' "$CMD" | tr '[:upper:]' '[:lower:]')"

# --- bq の書込系サブコマンド -------------------------------------------------
if printf '%s' "$lower" | grep -qE '(^|[;&|(]\s*)bq\s'; then
  if printf '%s' "$lower" | grep -qE 'bq\s+([a-z-]+\s+)*(load|cp|rm|mk|insert|update|partition)\b'; then
    verdict="write"
  elif printf '%s' "$lower" | grep -qE 'bq\s+([a-z-]+\s+)*query\b' \
    && printf '%s' "$lower" | grep -qE '\b(insert|update|delete|merge|create|drop|alter|truncate|replace)\b'; then
    verdict="write"
  else
    verdict="read"
  fi

  if [ "$verdict" = "write" ]; then
    # waca_loop_integration **だけ**を触っているなら許可（試作用の置き場）。
    # 「waca_loop_integration の字が入っていれば許す」ではない — それだと別のデータセットへ
    # 書きながら waca_loop_integration に言及するだけで抜けられる。
    # 本番データセット名を名指しで列挙し、waca_loop_integration 以外が1つでも出たら止める。
    if printf '%s' "$lower" | grep -q 'waca_loop_integration' \
       && ! printf '%s' "$lower" | grep -qE 'waca_loop_(grounding|ledger|knowledge|feedback)'; then
      exit 0
    fi
    deny "[WACA Loop G1] 本番BigQueryへの書込を止めました（既存データセットへの書込は管理者の承認後。試作は waca_loop_integration で行う）。

止めたコマンド: ${CMD}

進め方:
  1) まず --dry-run で内容を提示し、管理者の承認を取る
  2) 承認が出たら、そのセッションで WACA_LOOP_ALLOW_BQ_WRITE=1 を付けて実行する
（判定できない bq 書込系はフェイルクローズで block します。読み取り(SELECT)は通ります。）"
  fi
fi

# --- リポジトリ内の BigQuery 書込スクリプト（bq を経由しない経路） -------------
# 名指しの許可リストにする。「BigQuery を書きうる」判定を汎用に当てるのは無理なので、
# 実際に書くスクリプトを列挙し、増えたらここに足す運用にする（漏れは G1 の穴になるが、
# 推測で広く止めるより、狭く確実に止めて一覧を保守するほうが守れる）。
BQ_WRITER_SCRIPTS='seed_sfaq\.py|seed_terms\.py|seed_policy\.py|init_bigquery\.py|backfill_embeddings\.py|sync_persons_from_sheet\.py|preregister_from_sheet\.py'
# **実行しているときだけ**見る。ファイル名がコマンド行に現れただけで止めると
# `grep -n embed scripts/seed_sfaq.py` や `git add scripts/seed_sfaq.py` まで止まり、
# 「邪魔なガードは切られる」——切られたガードは何も守らない。
# 呼び方は一通りではない: `python scripts/x.py` / `python -m scripts.x` / `make <target>` /
# `python -c "...bigquery..."`。モジュール形式とインラインの BigQuery 書込も見る。**それでも網羅ではない**
# （新しいスクリプト・改名・ラッパ経由は抜ける）。README と仕様書はそう書く。
BQ_WRITER_MODULES='seed_sfaq|seed_terms|seed_policy|init_bigquery|backfill_embeddings|sync_persons_from_sheet|preregister_from_sheet' 
if printf '%s' "$lower" | grep -qE "(python3?|uv run|poetry run|make)[[:space:]]+[^;&|]*($BQ_WRITER_SCRIPTS)" \
   || printf '%s' "$lower" | grep -qE "python3?[[:space:]]+-m[[:space:]]+[a-z_.]*($BQ_WRITER_MODULES)" \
   || { printf '%s' "$lower" | grep -qE "python3?[[:space:]]+-c" \
        && printf '%s' "$lower" | grep -qE 'bigquery|\.query\(|insert_rows'; }; then
  # これらは --dry-run 付きなら上で既に許可されている。ここに来た＝本番に書く形。
  if ! printf '%s' "$lower" | grep -qE -- '(^|[[:space:]])--(help|version)([[:space:]]|=|$)'; then
    deny "[WACA Loop G1] 本番BigQueryに書くスクリプトの実行を止めました（本番への書込は管理者の承認後）。

止めたコマンド: ${CMD}

進め方:
  1) まず --dry-run で内容を提示し、管理者の承認を取る
  2) 承認が出たら WACA_LOOP_ALLOW_BQ_WRITE=1 を付けて実行する"
  fi
fi

# --- 既定が dry-run で、--apply* を付けたときだけ書き込むスクリプト ------------------
# 付けずに実行すれば確認だけなので通す。付けたときだけ止める。
BQ_APPLY_SCRIPTS='register_members|grant_role_unit|purge_frozen_ern'
if printf '%s' "$lower" | grep -qE "python3?[[:space:]]+([^;&|]*/)?(-m[[:space:]]+[a-z_.]*)?($BQ_APPLY_SCRIPTS)" \
   && printf '%s' "$lower" | grep -qE -- '(^|[[:space:]])--apply[a-z-]*([[:space:]]|=|$)'; then
  deny "[WACA Loop G1] 本番BigQueryに書くスクリプトの実行を止めました（本番への書込は管理者の承認後）。

止めたコマンド: ${CMD}

進め方:
  1) まず --apply を付けずに実行して内容を提示し、管理者の承認を取る
  2) 承認が出たら、そのセッションで WACA_LOOP_ALLOW_BQ_WRITE=1 を付けて実行する"
fi

# --- gcloud: 変更・デプロイは警告のみ（block はしない） ----------------------
if printf '%s' "$lower" | grep -qE '(^|[;&|(]\s*)gcloud\s' \
   && printf '%s' "$lower" | grep -qE '\b(deploy|create|delete|update|set-iam-policy|add-iam-policy-binding|replace)\b'; then
  notify "[WACA Loop G1] GCP の変更／デプロイに当たるコマンドです。CLAUDE.md の作業ルール4により管理者の承認が必要です（実行前に内容を提示してください）。"
fi
exit 0
