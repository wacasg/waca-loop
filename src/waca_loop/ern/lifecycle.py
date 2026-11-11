"""ERN のライフサイクル — frozen の宣言を畳んで、1年後に消す（SPEC_phase3 §5）.

純ロジック（INV-2）。I/O は `scripts/purge_frozen_ern.py` にある。

**弱点を隠さない。** purge の起点は `persons.updated_at` の代理で、専用の `frozen_at` 列を
持っていない。つまり**別要因で person 行が更新されると時計が延びる** — 退職から1年経っても
消えない行が出うる。これはコスト優先の暫定判断で、恒久化するなら列追加が要る
（SPEC_phase3 §7 の Open item）。`overdue` はこの前提の上での「消してよい候補」であって、
「退職から1年経った行」ではない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

#: frozen になってから削除するまで（SPEC_phase3 §5）。
RETENTION_DAYS = 365

#: frozen の宣言に付ける status。
FROZEN = "frozen"


@dataclass(frozen=True)
class PurgePlan:
    """何を畳み、何を消す候補にするか。**実行はしない**（既定 dry-run）."""

    #: frozen になった人の、まだ frozen でない宣言（→ status を frozen へ）
    to_freeze: tuple[str, ...] = ()
    #: 保持期間を過ぎた frozen 宣言（→ 削除の候補）
    overdue: tuple[str, ...] = ()
    #: 名簿に無い person_id を指す宣言（在籍も同意も確かめられない孤児）
    orphaned: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.to_freeze or self.overdue or self.orphaned)


def _age_days(when: datetime | None, now: datetime) -> float | None:
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return (now - when).total_seconds() / 86400.0


def plan_purge(
    assertions: list[dict],
    persons: dict[str, dict],
    *,
    now: datetime | None = None,
    retention_days: int = RETENTION_DAYS,
) -> PurgePlan:
    """削除計画を立てる（読むだけ）.

    ``assertions`` は ``[{"assertion_id","person_id","status"}, ...]``、
    ``persons`` は ``{person_id: {"status","updated_at"}}``。

    孤児（名簿に無い person_id）を **overdue と分けている**のが要点。両方「消す候補」だが、
    理由が違う — 孤児は「退職して1年経った」ではなく「名簿と宣言がずれている」で、
    消す前に**なぜずれたか**を見るべきものである。同じ山に入れると、その問いが消える。
    """
    now = now or datetime.now(UTC)
    to_freeze: list[str] = []
    overdue: list[str] = []
    orphaned: list[str] = []

    for row in assertions:
        pid = row.get("person_id")
        person = persons.get(pid)
        if person is None:
            orphaned.append(row["assertion_id"])
            continue
        if person.get("status") != FROZEN:
            continue
        if row.get("status") != FROZEN:
            to_freeze.append(row["assertion_id"])
            continue
        age = _age_days(person.get("updated_at"), now)
        if age is not None and age >= retention_days:
            overdue.append(row["assertion_id"])

    return PurgePlan(
        to_freeze=tuple(sorted(to_freeze)),
        overdue=tuple(sorted(overdue)),
        orphaned=tuple(sorted(orphaned)),
    )
