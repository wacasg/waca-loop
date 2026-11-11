"""申告の写し判定が「どの正規形で比べるか」を固定する（W1・純ロジック）.

`raise_lesson_from_task` の写し検査で壊れやすいのは、**判定の中身ではなく比較の正規形**
である。
`tests/test_s09_task_acl.py` は ServerContext 越しに「拒否されること」を見ているが、
拒否は写し判定・PII 判定・氏名判定のどれでも起きる。**どの門が閉じたのかを見ていない**
ため、正規形を壊しても他の門が拾って緑のままになる（下記4件はいずれも
`ServerContext` 経由のテストでは捕まらない）。

だからここでは `lesson_guard` の関数を直接呼び、門を1つずつ名指しで固定する。
`test_twin_conformance.py`（双子の形）とは対象が違う。
"""

from __future__ import annotations

from waca_loop.improvement.lesson_guard import borrows_evidence, names_in, normalize

_EVIDENCE = (
    "営業部の担当者が顧客の連絡先一覧をそのまま外部の生成AIに投入し、"
    "個人情報を含むファイルを社外へ持ち出す形になっていた"
)


def test_punctuation_cannot_break_the_window():
    """読点を刻んで窓判定を割る回避を塞ぐ.

    naive 版（NFKC と空白除去だけで、句読点・記号を落とさない正規形）だと、連続 32 文字が
    どこにも成立しなくなるので**この貼り付けは通る**。
    """
    evaded = "、".join(_EVIDENCE[i:i + 8] for i in range(0, len(_EVIDENCE), 8))
    assert evaded != _EVIDENCE
    assert borrows_evidence(f"今回の件を踏まえて考える。{evaded}。どう防ぐか？", [_EVIDENCE])


def test_full_width_cannot_break_the_window():
    """全角で打ち直した写しも写しである.

    英数字を含む申告を全角で貼り直すと、NFKC を通さない正規形では別の文字列になる。
    PII 検出器（`pii_kinds`）は自前で NFKC するので、連絡先が入っていれば別の門が拾って
    しまう — **連絡先を含まない**申告文でないと、この門の穴は見えない。
    """
    evidence = "担当者がCSV形式の会員データ2000件をAIツールへ投入して集計させていた"
    pasted = evidence.translate(
        {ord(c): ord(c) + 0xFEE0 for c in "CSVAI0123456789"}
    )
    assert pasted != evidence
    assert borrows_evidence(f"前提を整理する。{pasted}。以後どうするか。", [evidence])


def test_a_name_typed_with_spaces_is_still_a_name():
    """氏名の照合は本文と名簿を**同じ**正規形に畳んでから.

    「佐 藤 太 郎」は攻撃ではなく日本語の普通の打ち方。名簿をそのまま突き合わせる naive 版は
    素通りする。ここは `normalize` を共有していることの表明でもある。
    """
    roster = ("佐藤 太郎",)
    assert names_in("佐 藤 太 郎の件を踏まえ、資料の取り扱いを見直す", roster)
    assert normalize("佐 藤 太 郎") == normalize("佐藤太郎")
    assert not names_in("担当者の件を踏まえ、資料の取り扱いを見直す", roster)


def test_a_short_report_pasted_whole_is_a_copy():
    """窓幅に満たない申告でも、本文がほぼそれ自体なら写し.

    窓（32文字）だけで切る naive 版は、短い申告を丸ごと貼っても通す。割合（COPY_RATIO）で
    見ているからこそ捕まる。裏側（一般句の巻き添え）は
    `test_a_legitimate_generalization_is_not_blocked` が押さえているので、ここでは
    「短くても写しは写し」だけを見る。
    """
    short = "部門長がAIに会員名簿を貼り付けていた"
    assert len(short) < 32
    # 本文は evidence より長い（= 単なる切り出しではない）が、ほぼ evidence そのもの。
    # 窓だけで切る naive 版はここを通す。
    pasted = f"{short}、という報告があった。"
    assert borrows_evidence(pasted, [short])
    # 一般句が長い文の一部として現れるだけなら通す（塞ぎすぎない）
    assert not borrows_evidence(
        "対応が遅いと感じさせないために、初動でいつまでに返すかを最初に伝えることにする",
        ["対応が遅い"],
    )
