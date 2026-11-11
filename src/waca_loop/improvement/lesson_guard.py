"""申告の本文が「学び」に紛れ込んでいないかを見る (SPEC_phase2 §2.4).

``raise_lesson_from_task`` は改善タスク（＝誰かの申告）から組織の学びを起こす導線で、
起きた学びは **org 可視**で置かれる。だから申告の生の言葉——告発された人の名前・
連絡先・出来事の具体——が本文に残っていると、このツールは「誰が誰を告発したか」を
全社に配る装置になる。

``evidence in text`` の1行だけの検査では、次の穴が残る。ここではそれをすべて塞ぐ:

- **別タスクの evidence** — ひとつの申告から incident / harmful / nps_detractor の
  3タスクが派生し、それぞれ別の evidence を持つ。A のタスクIDを指しながら B の evidence を
  貼れば素通りする
- **部分貼り** — 完全一致の包含しか見ないと、前後に一言足すだけで抜ける
- **``first_feedback``** — フィードバック本文は埋め込みの主対象なので、見出しを一般化しても
  本文で意味検索に当たる

純 stdlib（INV-2）。
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# 部分貼りを捕まえる窓幅。
#
# 句読点を落として比べるため、窓が短いと一致が増えて**正当な言い換えの巻き添え**が起きる。例:
# 「AIを使う前に個人情報が含まれていないか確認する手順が守られていなかった」という申告に対し
# 「…確認する手順を、全案件の開始時チェックに入れる」という真っ当な学びが、業務日本語として
# ありふれた連なりを共有するだけで拒否されてしまう（窓幅 24 ではこれが起きる）。
#
# 32 文字を丸ごと共有していれば「写した」と言ってよく、そこまで一致しない言い換えは通す。
SHINGLE = 32

# 短い evidence を「写し」と見なす下限。本文の6割以上が evidence なら、短くても写しである。
COPY_RATIO = 0.6

_WS = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    """比較のための正規形（NFKC ＋ 空白除去）。

    NFKC は必須である。日本語入力の既定は全角なので、`ｓａｔｏ＠ｅｘａｍｐｌｅ．ｏｒｇ` は
    攻撃ではなく**普通に起きる打ち方**であり、畳まないと検査を素通りする。畳み方は
    ``anonymize.normalize_text`` に合わせる — 検出器と検査対象が別の正規形を見ていたら、
    揃っているように見えて揃っていない。
    """
    from waca_loop.ingest.anonymize import normalize_text

    folded = _WS.sub("", normalize_text((text or "").replace("　", " ")))
    # 句読点・記号も落とす。残すと `、` を20文字ごとに差し込むだけで窓判定が全部割れる
    # 意味は句読点に乗っていないので、
    # 比較の土俵から外すのが正しい。
    return "".join(c for c in folded if unicodedata.category(c)[0] not in ("P", "S"))


def borrows_evidence(text: str | None, evidences: list[str], *, window: int = SHINGLE) -> bool:
    """``text`` が ``evidences`` のどれかを写しているか（丸写し・部分貼りの両方）。

    ``evidences`` は「同じ申告から派生したタスク全部」を渡すこと。ひとつだけ渡すと、
    別タスクの evidence を貼る抜け道が残る。

    **短い evidence を「含む」だけで切らない。** `nps_reason` はそのまま evidence に
    なるので「対応が遅い」のような一般句が evidence になることがあり、包含で切ると
    **その語を含む正当な学びが永久に書けなくなる**（「誤送信を防ぐため宛先を二重確認
    する」が拒否される）。塞ぎすぎて誰も学びを起こさなくなるのは、漏れるのとは別の形の
    失敗である。

    かわりに **本文に占める割合**を見る。短い evidence でも、本文がほぼそれ自体なら
    写しである（「部門長がAIに会員名簿を貼り付けていた」は20文字だが、そのまま貼れば
    誰の話かがそのまま残る）。一般句が長い文の一部として現れるだけなら通す。
    """
    t = normalize(text)
    if not t:
        return False
    for raw in evidences:
        e = normalize(raw)
        if not e:
            continue
        if t in e:
            return True   # 本文が evidence の一部＝切り出して貼っただけ
        if e in t and (len(e) >= window or len(e) / len(t) >= COPY_RATIO):
            return True
        if len(e) >= window and len(t) >= window:
            if SequenceMatcher(None, e, t).ratio() >= 0.95:
                return True
        # 部分貼り: evidence の任意の連続 window 文字が本文に現れたら写しとみなす
        if len(e) >= window and any(
            e[i:i + window] in t for i in range(len(e) - window + 1)
        ):
            return True
    return False


def pii_kinds(text: str | None) -> list[str]:
    """本文に残っている個人情報の**種別名**（値は返さない — FLOOR-01）。

    検出器は ``ingest.anonymize.detectors()`` を使い回す（正規表現の二重管理を作らない）。
    検査は正規形に対して行う — 全角で書かれた連絡先を見落とさないため。
    """
    from waca_loop.ingest.anonymize import detectors, normalize_text

    body = normalize_text(text or "")
    return sorted({kind for kind, pattern in detectors() if pattern.search(body)})


def names_in(text: str | None, roster_names: tuple[str, ...]) -> bool:
    """在籍者の氏名が本文に残っているか。

    ``detectors()`` に氏名の検出器は無い（氏名は正規表現では決まらず、名簿で決まる）。
    そのため「佐藤部長の件を踏まえ…」は連絡先が無ければ素通りしてしまう。判定は
    ``anonymize.scrub_text`` に名簿を渡して
    委ねる — 表記ゆれの展開はあちらが正典で、ここで書き直さない。

    **氏名そのものは返さない**（真偽だけ）。
    """
    if not text or not roster_names:
        return False
    from waca_loop.ingest.anonymize import scrub_text

    # 本文も名簿も ``normalize`` の同じ正規形に畳んでから突き合わせる。畳まないと
    # 「佐 藤 太 郎」（分かち書き — 攻撃ではなく日本語の普通の打ち方）で抜ける。
    # 全角を畳むのと同じ理由が、そのまま空白と記号にも当てはまる。
    folded_roster = tuple(n for n in (normalize(n) for n in roster_names) if n)
    return "name" in scrub_text(normalize(text), roster_names=folded_roster).findings
