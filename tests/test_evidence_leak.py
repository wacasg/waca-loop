"""raise_lesson_from_task の写し込みガード (§2.4)。

固定する2つの性質:
(1) `first_feedback` もガードを通ること
(2) ガードは完全一致ではなく、改行1つ・部分写しでも抜けないこと

どちらも `search_sfaq` が本文のまま全 org メンバーへ返す欄に、申告者が書いた
「誰が何をやったか」がそのまま流れる経路になりうる。
"""

from __future__ import annotations

import pytest

from waca_loop.server import evidence_leak

EVIDENCE = (
    "田中太郎(tanaka@example.net) が会員名簿CSVをChatGPTに貼りました。"
    "至急止めてください。"
)


def test_full_paste_is_refused():
    assert evidence_leak(EVIDENCE, f"学び: {EVIDENCE}") is True


def test_an_inserted_newline_does_not_slip_through():
    """完全一致の穴 — 改行を1つ挟むだけで抜けないこと."""
    mangled = EVIDENCE.replace("が会員名簿", "が\n会員名簿")
    assert evidence_leak(EVIDENCE, f"学び: {mangled}") is True


def test_partial_paste_carrying_name_and_contact_is_refused():
    """部分写しの穴 — 氏名と連絡先だけを抜き出す貼り方も拒否すること."""
    partial = "学び: 田中太郎(tanaka@example.net) が会員名簿CSVを貼った件から"
    assert evidence_leak(EVIDENCE, partial) is True


def test_a_genuine_generalization_passes():
    """一般化された学びは通ること — 通らないなら道具として使えない."""
    lesson = "会員名簿のような個人情報を含む一覧を、安全確認のない外部AIに貼らない。"
    assert evidence_leak(EVIDENCE, lesson) is False


@pytest.mark.parametrize("text", [None, "", "   "])
def test_empty_text_is_not_a_leak(text):
    assert evidence_leak(EVIDENCE, text) is False


def test_no_evidence_never_refuses():
    assert evidence_leak("", "なんでも書ける") is False


def test_short_evidence_narrows_the_window_instead_of_giving_up():
    """短い申告でも部分写しを見る — 包含に落とすと丸ごと以外が素通りする."""
    short = "田中太郎が会員名簿を貼った"
    assert evidence_leak(short, f"学び: {short}の件") is True
    assert evidence_leak(short, "学び: 田中太郎が会員名簿を貼ったと聞いた") is True
    assert evidence_leak(short, "個人情報を含む一覧を安全確認のない環境に入れない") is False


def test_a_bare_short_name_is_NOT_caught_and_that_is_documented():
    """守備範囲の外を固定する — 直したつもりにならないためのテスト.

    「田中太郎が名簿を貼った」から氏名4文字だけを残されても当たらない。窓幅を4まで
    下げれば当たるが、そのとき「名簿を貼らない」という正当な一般化まで弾いてしまう。
    氏名を見分けるのは固有表現の問題で、重なり判定では解けない (evidence_leak の docstring)。
    """
    assert evidence_leak("田中太郎が名簿を貼った", "学び: 田中太郎の件から") is False
