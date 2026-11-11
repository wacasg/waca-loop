"""ERN 出力の許可リスト関門（E1・SPEC_phase3 §3）.

**「出力に数値を出さない」は、将来のツール追加でいちばん壊れやすい。** 誰かが親切心で
`match_count` や `score` を足した瞬間、ERN は「評価ではなくマッチング」（INV-5）でなくなる。
しかもそれは1行の追加で起き、レビューでは見落としやすい。

だから禁止を**共通ヘルパ1つ**にして、全 ERN ツールの出口で必ず通す。ツールが増えるたびに
書き足すのではなく、ここを通らないと外に出られない形にしておく。

やり方は denylist ではなく **allowlist**。denylist は fail-open で、新しい名前
（`confidence` / `weight` / `hits`…）を思いつくたびに追加が要る — 追いつかない。
"""

from __future__ import annotations

from typing import Any

#: 候補1件が持ってよいキー。**これ以外は足さない**（SPEC_phase3 §3 の戻り値定義）。
CANDIDATE_KEYS: frozenset[str] = frozenset({
    "person_id",
    "display_name",
    "units",
    "topic_match",      # band。数値でない
    "freshness",        # band。数値でない
    "declared_topics",
    "evidence_type",    # 実績ベース|役職ベース（E10）
})

#: 応答トップレベルが持ってよいキー。
RESPONSE_KEYS: frozenset[str] = frozenset({"candidates"})


class ScoreLeak(AssertionError):
    """許可リストの外のキー、または数値が候補に混ざった。**握り潰さない。**"""


def _is_number(value: Any) -> bool:
    # bool は int の subclass だが、真偽は比較可能な「点数」ではないので除く。
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def assert_no_scores(response: dict) -> dict:
    """ERN ツールの応答を検査して、そのまま返す（通らなければ送出）.

    2つを見る:

    1. **キーが許可リストの中にあるか。** 名前で禁止する（`score` を弾く）のではなく、
       名前で許可する。新しいキーは、ここに足す＝仕様を変えるという意思表示を伴う。
    2. **候補の値に数値が無いか。** キー名が無難でも（`topic_match` が 0.87 でも）、
       比較できる数が出た時点で順位が読める。band に畳むという §3 の約束は、
       名前ではなく**値の型**で守られる必要がある。

    リストの入れ子（`units` / `declared_topics`）は文字列だけを許す。
    """
    extra = set(response) - RESPONSE_KEYS
    if extra:
        raise ScoreLeak(f"応答に許可されていないキー: {sorted(extra)}")

    for i, cand in enumerate(response.get("candidates") or []):
        if not isinstance(cand, dict):
            raise ScoreLeak(f"candidates[{i}] が dict でない: {type(cand).__name__}")
        unknown = set(cand) - CANDIDATE_KEYS
        if unknown:
            raise ScoreLeak(
                f"candidates[{i}] に許可されていないキー: {sorted(unknown)} "
                f"(許可: {sorted(CANDIDATE_KEYS)})"
            )
        for key, value in cand.items():
            if _is_number(value):
                raise ScoreLeak(
                    f"candidates[{i}].{key} が数値 ({value!r})。"
                    "ERN は帯(band)しか出さない — 数が出た時点で順位が読める (E1/INV-5)"
                )
            if isinstance(value, (list, tuple)):
                for j, item in enumerate(value):
                    if not isinstance(item, str):
                        raise ScoreLeak(
                            f"candidates[{i}].{key}[{j}] が文字列でない: {item!r}"
                        )
    return response
