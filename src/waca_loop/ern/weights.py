"""ERN の係数 — **1箇所に集める**（SPEC_phase3 §2.3・E9）.

ロジックの中に数値リテラルを置かない。理由は2つ:

1. **E9**: 半減期は config 由来でなければならない。`engine.py` に `180` と書いた瞬間、
   設定を変えても候補が変わらなくなり、受け入れ基準が形骸化する。
2. これらは **Open item**（SPEC_phase3 §7）である。データが貯まってから調整する前提の値が
   コードに散っていると、「どこを直せば効くのか」が読めなくなる。

値そのものは暫定。調整するときは、この dict と config だけを触れば済むようにしてある。
"""

from __future__ import annotations

from waca_loop.models import FbType

# fb_type → 重み。反証だけが減算側（SPEC_phase3 §2.3 ①）。
#
# `FbType` を鍵にしているのは、文字列で書くと綴り違いが**黙って重み0**になるから
# （その fb_type は集計から消えるが、誰も気づかない）。列挙型なら import の時点で落ちる。
WEIGHTS: dict[FbType, float] = {
    FbType.DECISION: 3.0,      # 採用された判断
    FbType.SUPPLEMENT: 1.5,    # 訂正・補足が採られた
    FbType.USEFUL: 1.0,        # 参照された
    FbType.AGREE: 0.5,         # 同意された
}

# 反証は減算する（SPEC_phase3 §2.3 ①の第2項）。
W_REFUTED: float = 2.0

# 重みを持たない fb_type の既定。**0.0 にはしない** — 新しい fb_type が増えたとき、
# 黙って無視されるより「参照された」相当で数えるほうが、取りこぼしに気づきやすい。
# ここを 0.0 にしたいなら、それは管理者の判断が要る変更である。
W_DEFAULT: float = 1.0


def weight_for(fb_type: FbType) -> float:
    """その fb_type の重み。反証は呼び出し側で減算するので、ここでは正の値を返す."""
    if fb_type is FbType.REFUTE:
        return W_REFUTED
    return WEIGHTS.get(fb_type, W_DEFAULT)


# --- band（帯）の境目 -----------------------------------------------------------------------
#
# **外に出るのはここから先だけ**（SPEC_phase3 §3）。スコア生値は出さず、band に畳んでから返す。
# 境目を weights に置いているのは、これも「調整する値」だからで、engine 側にリテラルを
# 置かないという同じ理由による。

# 専門度の帯: しきい値以上なら「近い」、それ未満（かつシグナルあり）なら「やや近い」。
TOPIC_MATCH_STRONG: float = 3.0
BAND_TOPIC_STRONG = "近い"
BAND_TOPIC_WEAK = "やや近い"

# 鮮度の帯: 直近のシグナルが何日以内なら「最近」か。
FRESHNESS_RECENT_DAYS: int = 90
BAND_FRESH_RECENT = "最近"
BAND_FRESH_OLD = "しばらく前"

# 根拠種別（E10）。
EVIDENCE_SIGNAL = "実績ベース"
EVIDENCE_ROLE = "役職ベース"
