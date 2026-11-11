"""シグナルの導出 — `<DS_KNOWLEDGE>.feedback` の既存行から（SPEC_phase3 §1・§2.3）.

**新テーブルを作らない。** 専門度も貢献量も、`feedback` の
（`fb_type` × `created_at` × `author_id`）からの導出値である。シグナル表を別に持つと、
同じ事実の写しが2つになり、片方だけ古くなる — そして「誰がどれだけ貢献したか」を
記録した表が独立に存在すること自体が、INV-5（評価ではなくマッチング）から遠い。

ここは**純ロジック**（INV-2）。BigQuery も MCP も import しない。呼び出し側が
「viewer に見える問い」と「その問いに付いたフィードバック」を渡す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from waca_loop.ern.weights import W_REFUTED, weight_for
from waca_loop.models import FbType, Feedback


@dataclass
class PersonSignals:
    """1人 × 1領域の集計。**外に出さない中間値**（SPEC_phase3 §2.3 注）."""

    person_id: str
    #: ① 専門度（減衰つき加重和 − 反証）
    expertise: float = 0.0
    #: ② 貢献量（減衰なしの素の行数）。コールドスタート判定にだけ使い、**出力しない**
    contribution: int = 0
    #: 直近シグナルの経過日数。鮮度の帯にだけ使う
    days_since_latest: float | None = None
    #: 集計に使ったフィードバックの id。監査のためであって、応答には出さない
    feedback_ids: list[str] = field(default_factory=list)


def age_days(created_at: datetime, now: datetime) -> float:
    """経過日数。tz を持たない値は UTC とみなす（BigQuery 由来は aware、テスト由来は naive）."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return max(0.0, (now - created_at).total_seconds() / 86400.0)


def decay(age: float, half_life_days: float) -> float:
    """0.5 ^ (t / half_life)（SPEC_phase3 §2.3）.

    `half_life_days` は **config 由来**（E9）。既定値をここに書かない — 書くと
    「設定を変えても候補が変わらない」状態が作れてしまい、E9 が形骸化する。
    """
    if half_life_days <= 0:
        # 半減期0以下は「減衰させない」と読む。ゼロ除算で落とすより、設定ミスが
        # 候補の消失ではなく「全部同じ重み」として現れるほうが気づける。
        return 1.0
    return 0.5 ** (age / half_life_days)


def aggregate(
    feedbacks: list[Feedback],
    *,
    half_life_days: float,
    now: datetime,
) -> dict[str, PersonSignals]:
    """author_id ごとに ①専門度 と ②貢献量 を集計する.

    **渡された行しか見ない。** 可視範囲の絞り込み（Gate1）と著者マスク行の除外（§4a）は
    呼び出し側の責任で、ここへ来る時点で済んでいる前提。この関数に ACL を持たせると、
    ゲートが2箇所に分かれて片方だけ緩む — §3.3 が避けている形そのものになる。

    `author_id` が None の行は落とす。組織が著者の行（移行由来の判断など）を誰かの
    実績にすると、実在しない専門家が立つ。
    """
    out: dict[str, PersonSignals] = {}
    for fb in feedbacks:
        author = fb.author_id
        if not author:
            continue
        sig = out.setdefault(author, PersonSignals(person_id=author))
        age = age_days(fb.created_at, now)
        d = decay(age, half_life_days)
        if fb.fb_type is FbType.REFUTE:
            sig.expertise -= W_REFUTED * d
        else:
            sig.expertise += weight_for(fb.fb_type) * d
        sig.contribution += 1
        sig.feedback_ids.append(fb.feedback_id)
        if sig.days_since_latest is None or age < sig.days_since_latest:
            sig.days_since_latest = age
    return out
