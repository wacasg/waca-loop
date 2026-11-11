"""Tool-event trail for coverage monitoring (SPEC_ops R3a).

The server cannot observe "the turn where log_usage was NOT called" directly, so it records
the observable half: every ``get_applicable_policy`` call, every ledger write, and every scope
declaration become one thin event row. ``scripts/coverage_report.py`` then counts policy-checks
that never got a matching ledger write (per actor per day) — the R3a mismatch signal.

Rows carry NO content: tool name, actor attribution (same ``actor_id``/``actor_raw`` split as
the ledger, §5) and an optional short ``detail`` (e.g. the declared scope value). Recording is
best-effort — a trail failure must never break the tool call itself (callers swallow errors).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings

# Event vocabulary (detail-free unless noted):
#   policy_check    — get_applicable_policy was served
#   usage_recorded  — log_usage wrote a row (recorded or pending)
#   scope_declared  — set_session_scope (detail = the scope value only, never the reason)
#   usage_reviewed  — review_pending_usage (detail = "(approve|discard):<log_id>"). The discard
#                     branch physically deletes the ledger row, so this event is the ONLY
#                     in-system record that it ever existed — and for that branch alone the
#                     write is fail-closed, not best-effort (SPEC_ops R3a 例外).
#   usage_withdrawn / usage_withdraw_refused
#                   — request_log_deletion (detail = "<reason>:<log_id>"). Written AFTER the
#                     store call, so the event states the outcome and never asserts a
#                     withdrawal that did not happen. Best-effort on purpose: withdrawal
#                     destroys nothing, so fail-closed would only keep private content
#                     visible during an observability outage. The `refused` variant is what
#                     makes a sweep across other people's log_ids visible.
#   usage_resend_deduped
#                   — log_usage recognised a resend and wrote no new row. Like
#                     usage_write_failed, deliberately NOT in _COVERAGE_TOOLS: a resend is
#                     not a newly recorded turn, so counting it would let one reused
#                     `request_id` report five recorded turns backed by one summary.
#   usage_write_failed
#                   — the ledger write itself raised. Best-effort,
#                     and deliberately NOT in _COVERAGE_TOOLS: a failed write is not a
#                     record, so it must not close a policy_check in the nudge. Its point is
#                     that "the client tried and the ledger lost it" leaves a row somewhere,
#                     so a mismatch can be told apart from a swallowed outage.
#   withdrawn_usage_listed / withdrawn_usage_read
#                   — the steward's investigation path (SPEC_ops R6). `listed` carries
#                     the count only (no content crosses); `read` carries the one `log_id`
#                     and is fail-closed, because "which private summary did the steward
#                     read" must have an answer.
# None of the four join _COVERAGE_TOOLS: coverage asks "is this person's AI use being
# recorded", and a withdrawal is the opposite of a use.


# The two events C4 pairs up. Kept next to the vocabulary above so a new event kind has to
# decide, explicitly, whether it belongs in the coverage story.
_COVERAGE_TOOLS = ("policy_check", "usage_recorded")

#: How many recent policy checks the nudge looks back over (SPEC_coverage §3 C4 の例に合わせる).
COVERAGE_HINT_WINDOW = 5

#: How far back ``recent_by_actor`` looks. Bounds the partition scan, and matches the nudge's
#: purpose: advice about the last few turns, not a standing record of someone's habits.
RECENT_WINDOW_DAYS = 7

#: The exact shape ``recent_by_actor`` returns, on BOTH twins. Named once so the SELECT list
#: and the in-process projection cannot drift into returning different columns — a divergence
#: the twins-agree test would otherwise have to re-discover for every new caller.
#:
#: ``detail`` is deliberately NOT here. C4 reads only ``tool``, and ``tools=`` is a
#: caller-settable parameter: widening it to ``scope_declared`` would otherwise start pulling
#: scope values out of the trail for a feature that never needed them. Extract what the
#: question needs, not what the row happens to hold (FLOOR-02 の流儀).
_RECENT_FIELDS = ("event_id", "occurred_at", "tool", "actor_id", "actor_raw")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def row_ts(row: dict) -> datetime | None:
    """A row's ``occurred_at`` as an aware datetime, or ``None`` when it cannot be read.

    ``None`` means **dropped**, because every twin's window is a SQL predicate
    (``occurred_at >= …``) that no NULL or malformed TIMESTAMP satisfies. Keeping such a row
    in memory only would make the two stores disagree for exactly the rows nobody replays.

    Public and shared: ``ledger.usage_log`` asks the same question of the same column, and
    two copies of this rule agreeing by comment is how they eventually stop agreeing.
    """
    raw = row.get("occurred_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _within(row: dict, cutoff: datetime) -> bool:
    ts = row_ts(row)
    return ts is not None and ts >= cutoff


def same_actor(row: dict, actor_id: str | None, actor_raw: str | None) -> bool:
    """Mirror of the BigQuery WHERE clause — resolved actors match on ``actor_id`` only.

    Public because it is *both* gates of the §3.3 pair: the in-process store filters with it,
    and ``ServerContext._coverage_hint`` re-checks the BigQuery twin's rows with it. Keeping
    one function on both sides is what stops the two from drifting apart.
    """
    # Falsy, not ``is None``: an empty-string identity must never form a bucket that a second
    # empty-string caller could join. There is no wiring that produces one today (hosted mode
    # fails closed on an unresolved sub), but "no identity" and "identity ''" must not differ.
    if actor_id:
        return row.get("actor_id") == actor_id
    return bool(actor_raw) and row.get("actor_raw") == actor_raw


def _event_row(
    tool: str,
    actor_id: str | None,
    actor_raw: str | None,
    detail: str | None,
    occurred_at: datetime,
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "occurred_at": occurred_at.isoformat(),
        "tool": tool,
        "actor_id": actor_id,
        "actor_raw": actor_raw,
        "detail": detail,
        "created_at": occurred_at.isoformat(),
    }


class InMemoryToolEventStore:
    """In-memory event trail (tests / offline)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(
        self,
        tool: str,
        *,
        actor_id: str | None = None,
        actor_raw: str | None = None,
        detail: str | None = None,
        occurred_at: datetime | None = None,
    ) -> str:
        row = _event_row(tool, actor_id, actor_raw, detail, occurred_at or _utcnow())
        self.rows.append(row)
        return row["event_id"]

    def recent_by_actor(
        self,
        *,
        actor_id: str | None,
        actor_raw: str | None,
        tools: tuple[str, ...] = _COVERAGE_TOOLS,
        limit: int = 40,
    ) -> list[dict]:
        """This ONE actor's recent events, oldest first (C4 nudge — SPEC_coverage §3 C4).

        Scoped to a single actor by construction: the caller passes its own attribution from
        :func:`ledger_actor`, never a client-supplied value, and there is no parameter that
        could widen the query to anyone else. See the BigQuery twin for the same rule in SQL.
        """
        # The BigQuery twin bounds its scan to RECENT_WINDOW_DAYS,
        # so the in-memory twin has to drop the same rows. Without this, an old row that
        # BigQuery can no longer see still closes (or opens) a check offline, and the two
        # stores disagree about the nudge for exactly the turns nobody replays.
        cutoff = _utcnow() - timedelta(days=RECENT_WINDOW_DAYS)
        hits = [
            r for r in self.rows
            if r["tool"] in tools
            and same_actor(r, actor_id, actor_raw)
            and _within(r, cutoff)
        ]
        hits.sort(key=lambda r: (str(r["occurred_at"]), str(r["event_id"])))
        return [{k: r.get(k) for k in _RECENT_FIELDS} for r in hits[-limit:]]


class BigQueryToolEventStore:
    """Production event trail backed by BigQuery streaming inserts."""

    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def record(
        self,
        tool: str,
        *,
        actor_id: str | None = None,
        actor_raw: str | None = None,
        detail: str | None = None,
        occurred_at: datetime | None = None,
    ) -> str:
        row = _event_row(tool, actor_id, actor_raw, detail, occurred_at or _utcnow())
        errors = self._client.insert_rows_json(self._settings.tool_events_table, [row])
        if errors:
            raise RuntimeError(f"tool_events insert failed: {errors}")
        return row["event_id"]

    def recent_by_actor(
        self,
        *,
        actor_id: str | None,
        actor_raw: str | None,
        tools: tuple[str, ...] = _COVERAGE_TOOLS,
        limit: int = 40,
    ) -> list[dict]:
        """BigQuery twin of :meth:`InMemoryToolEventStore.recent_by_actor` (C4).

        The actor predicate is the ACL here: the WHERE clause pins the query to exactly one
        actor, bound as a parameter, so **no other viewer's rows can leave the database** —
        the same "forbidden rows never leave BQ" shape as §3.3. ``actor_raw`` is only consulted
        when the viewer is unresolved (``actor_id IS NULL``), matching the ledger's §5 split;
        without that guard, a NULL ``actor_id`` would match every other unresolved caller.
        """
        from google.cloud import bigquery

        job = self._client.query(
            f"""
            SELECT {", ".join(_RECENT_FIELDS)}
            FROM `{self._settings.tool_events_table}`
            WHERE tool IN UNNEST(@tools)
              -- The table is PARTITION BY DATE(occurred_at); without this predicate every
              -- policy check full-scans the org's whole trail, so a per-turn nudge would
              -- get slower and dearer as the org gets busier.
              AND occurred_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
              AND ((@actor_id IS NOT NULL AND actor_id = @actor_id)
                OR (@actor_id IS NULL AND @actor_raw IS NOT NULL AND actor_raw = @actor_raw))
            -- event_id breaks ties so two events sharing a timestamp cannot order one way
            -- here and the other way in the in-memory twin (a tie would otherwise flip a
            -- recorded turn into a reported miss on BigQuery only).
            ORDER BY occurred_at DESC, event_id DESC
            LIMIT @limit
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("tools", "STRING", list(tools)),
                    bigquery.ScalarQueryParameter("actor_id", "STRING", actor_id),
                    bigquery.ScalarQueryParameter("actor_raw", "STRING", actor_raw),
                    bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
                    bigquery.ScalarQueryParameter("days", "INT64", int(RECENT_WINDOW_DAYS)),
                ]
            ),
        )
        rows = [dict(r) for r in job.result()]
        rows.sort(key=lambda r: (str(r["occurred_at"]), str(r["event_id"])))
        return rows


def coverage_mismatch(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Pure R3a aggregation: per (day, actor) → checks / writes / mismatch.

    ``mismatch = max(0, policy_check − usage_recorded)`` — "confirmed the policy but never
    logged" suspicion count. Used by scripts/coverage_report.py and tested offline.

    ``silent = 1`` when the actor RETRIEVED that day and recorded nothing at all (R3b). This
    is the case ``mismatch`` structurally cannot see: a client that reads the org's knowledge
    and calls neither ``get_applicable_policy`` nor ``log_usage`` leaves both counters at 0,
    so ``mismatch`` is 0 and the day looks clean. It is not clean — it is a connection using
    the corpus entirely outside the ledger, which is the same blind spot as shadow AI except
    the actor is a KNOWN one (e.g. a Claude session with hours of retrieval and one ledger
    row).

    Counts are deliberately not differenced: one turn legitimately fans out to several
    retrievals and a single ``log_usage``, so ``retrieval − usage_recorded`` is noise. The
    signal is the zero.
    """
    out: dict[str, dict[str, int]] = {}
    counted = ("policy_check", "usage_recorded", "retrieval")
    for row in rows:
        day = str(row["occurred_at"])[:10]
        actor = row.get("actor_id") or row.get("actor_raw") or "?"
        key = f"{day}/{actor}"
        bucket = out.setdefault(
            key,
            {"policy_check": 0, "usage_recorded": 0, "retrieval": 0,
             "mismatch": 0, "silent": 0},
        )
        if row["tool"] in counted:
            bucket[row["tool"]] += 1
    for bucket in out.values():
        bucket["mismatch"] = max(0, bucket["policy_check"] - bucket["usage_recorded"])
        bucket["silent"] = int(bucket["retrieval"] > 0 and bucket["usage_recorded"] == 0)
    return out


# --- C1: 登録者 × 接続実績（SPEC_coverage §3 C1）---------------------------------------------


def connection_funnel(
    registrations: list[dict], events: list[dict], ledger_rows: list[dict] | None = None
) -> list[dict]:
    """登録者を起点に、接続実績を左結合して返す。**未接続を先頭に。**

    既存の :func:`coverage_mismatch` は `tool_events` を起点にするので、**接続した人しか
    見えない**。登録だけして一度も繋いでいない人はレポートに現れず、気づく手段がない
    （SPEC_coverage §1）。
    記録率を上げる施策は、使っている人が少数のままでは全体を動かさない。だから**登録者側から
    見る**。

    並びは「未接続が先、その中では登録が古い順」— 古いほど放置されている。
    ``person_id`` 以外の識別子（メール等）はここでは扱わない。マスクが要るのは拒否ログ側で、
    そちらは :func:`rejected_subject_summary`。
    """
    ledger_count: dict[str, int] = {}
    for row in ledger_rows or []:
        actor = row.get("actor_id") or row.get("actor_raw") or "?"
        ledger_count[actor] = ledger_count.get(actor, 0) + 1

    seen: dict[str, dict] = {}
    for row in events:
        actor = row.get("actor_id") or row.get("actor_raw") or "?"
        day = str(row.get("occurred_at") or "")[:10]
        b = seen.setdefault(actor, {"events": 0, "first_event": None, "last_event": None})
        b["events"] += 1
        if day:
            b["first_event"] = min(b["first_event"] or day, day)
            b["last_event"] = max(b["last_event"] or day, day)

    out: list[dict] = []
    for reg in registrations:
        pid = reg.get("person_id") or "?"
        hit = seen.get(pid, {"events": 0, "first_event": None, "last_event": None})
        out.append({
            "person_id": pid,
            "registered_on": str(reg.get("registered_on") or "")[:10] or None,
            "claimed": bool(reg.get("claimed_at")),
            "events": hit["events"],
            "first_event": hit["first_event"],
            "last_event": hit["last_event"],
            "ledger_rows": ledger_count.get(pid, 0),
            "connected": hit["events"] > 0,
        })
    out.sort(key=lambda r: (r["connected"], r["registered_on"] or "", r["person_id"]))
    return out


def funnel_summary(funnel: list[dict]) -> dict:
    """R7 — :func:`connection_funnel` の行を**段の集計**に畳む（SPEC_coverage §3 C5・AC-5）.

    利用は「記録率より**手前**」（登録したが一度も接続していない段）で落ちうる。だから
    見るべきは記録率という単独の数字ではなく、
    **登録 → 接続 → 記録**の各段でどれだけ落ちるかである。記録率だけを追うと、使っている人が
    少数のままでいくら改善しても全体が動かない理由が最後まで見えない。

    **前提**: 各行の ``ledger_rows`` は **R6 の取り下げを除いた**台帳行数であること
    （`scripts/coverage_report.py` の `build_ledger_sql` が anti-join したうえで渡している）。
    生の台帳行数を渡すと、取り下げた人が記録者として残る。
    もう一点、``ledger_rows`` は `actor_id` を `person_id` に突き合わせて数えている。
    未解決の閲覧者が書いた行は `actor_raw`（ハッシュ）で入るので**どの person_id にも一致せず**、
    その人は記録ゼロ＝「接続したが書いていない」に見える。ホスト実行は未解決 sub に対して
    fail closed なので現状は届かないが、この数字が人に紐づく判断へ使われる以上は覚えておくこと。

    **`recording_rate` は「人」の通過率であって、ターンあたりの記録率ではない。**
    真の記録率＝記録された回数 ÷ AIとやり取りしたターン数は、分母がサーバから観測できない。
    それを埋めるのが C5 のターンカウンタで、**まだ無い**。取り違えると「記録率9割」と読めて
    しまうので、`per_turn_rate_available` を False で明示して返す。ここを True にしてよいのは
    ターンカウンタが入ったときだけ。
    """
    registered = len(funnel)
    connected = [f for f in funnel if f.get("connected")]
    # **記録は接続の部分集合として絞る。** funnel 全体から独立に抽出すると、`usage_recorded`
    # イベントだけ取りこぼして台帳行が残った人（tool_events の書き込みは best-effort で失敗を
    # 握り潰す — このモジュール冒頭のとおり）が「未接続なのに記録者」になり、記録段が接続段を
    # 上回る（通過率 200%・`connected_but_silent` = -1 のような値になる）。
    # ファネルは単調でなければ意味を成さないので、段の定義側で保証する。
    recording = [f for f in connected if (f.get("ledger_rows") or 0) > 0]

    def _rate(num: int, den: int) -> float | None:
        return round(num / den, 4) if den else None

    return {
        "stages": [
            {
                "stage": "registered",
                "label": "登録",
                "people": registered,
                "passed_from_prev": None,
            },
            {
                "stage": "connected",
                "label": "接続",
                "people": len(connected),
                "passed_from_prev": _rate(len(connected), registered),
            },
            {
                "stage": "recording",
                "label": "記録",
                "people": len(recording),
                "passed_from_prev": _rate(len(recording), len(connected)),
            },
        ],
        # 「接続した人のうち、台帳に1行でも書いた人の割合」。人ベース。
        "recording_rate": _rate(len(recording), len(connected)),
        # 登録から見た端から端まで。いちばん手前の落ちが効いてくるのはこちら。
        "end_to_end_rate": _rate(len(recording), registered),
        "never_connected": registered - len(connected),
        # 接続はしたが1行も書いていない人。C4 の nudge が届くべき相手。
        "connected_but_silent": len(connected) - len(recording),
        # C5 が入るまで False。ターンあたりの記録率は**まだ出せない**。
        "per_turn_rate_available": False,
    }


def rejected_subject_summary(log_entries: list[dict]) -> list[dict]:
    """許可リストで弾かれた接続試行を、sub ごとにまとめる（**値はマスクして返す**）。

    「登録されていないので弾いた」は、サーバから見れば正常動作だが、**本人から見ると
    毎回繋がらない**。同一 sub が何度も弾かれていても、まとめて見なければ気づけない
    （SPEC_coverage §1）。登録が接続試行の後追いになっている兆候はここにしか出ない。

    **FLOOR-01**: 拒否ログにはメールアドレスが生で出る。マスクは
    :mod:`waca_loop.ingest.anonymize` の ``scrub_text`` に委ねる — 正規表現をここで
    書き直すと二重管理になり、片方だけ直る日が来る（docs/HOOKS_SPEC.md G5 と同じ理由）。
    生値が要るときは Cloud Run のログを直接見る運用にする。
    """
    from waca_loop.ingest.anonymize import scrub_text

    by_sub: dict[str, dict] = {}
    for entry in log_entries:
        sub = str(entry.get("sub") or "?")
        day = str(entry.get("occurred_at") or "")[:10]
        b = by_sub.setdefault(sub, {"sub": sub, "attempts": 0, "days": set(), "hint": ""})
        b["attempts"] += 1
        if day:
            b["days"].add(day)
        text = entry.get("text") or ""
        if text and not b["hint"]:
            b["hint"] = scrub_text(text).text.strip()[:120]
    out = [
        {
            "sub": b["sub"][:12] + "…" if len(b["sub"]) > 12 else b["sub"],
            "attempts": b["attempts"],
            "first_day": min(b["days"]) if b["days"] else None,
            "last_day": max(b["days"]) if b["days"] else None,
            "masked_hint": b["hint"],
        }
        for b in by_sub.values()
    ]
    out.sort(key=lambda r: (-r["attempts"], r["last_day"] or ""))
    return out


# --- C4: サーバー側 nudge（SPEC_coverage §3 C4）------------------------------------------------


def coverage_hint(rows: list[dict], window: int = COVERAGE_HINT_WINDOW) -> str | None:
    """One advisory line about **the caller's own** unrecorded policy checks, or ``None``.

    ``rows`` are one actor's ``policy_check``/``usage_recorded`` events, oldest first (that is
    what :meth:`InMemoryToolEventStore.recent_by_actor` and its BigQuery twin return). A check
    counts as *recorded* when a ledger write follows it before the next check — the same
    pairing ``coverage_mismatch`` does per day, but per turn and per actor, because a nudge
    addressed to one person has to be about that person's last few turns, not a daily total.

    Two deliberate biases, both toward silence:

    - **The newest check is never counted** when nothing follows it. ``get_applicable_policy``
      runs *before* ``log_usage`` by design (運用ルール1), so the check for the very turn being
      served is structurally open. Counting it would make the nudge fire on every first call
      of every session — advice that is always on is noise, and noise is what the hooks in
      §3 C3 already have to fight.
    - **Nothing is said when nothing is missing**, and nothing is said with fewer than two
      completed checks to look at: a hint drawn from a single data point is a guess.

    **Known limit — one check per turn is assumed.** Turn boundaries are not observable from
    the server at all (that is what C5's turn counter is for, §3 C5). A turn that consults the
    policy twice and logs once therefore leaves one open check and is reported as a miss. The
    obvious cure — collapsing consecutive checks — is worse: it also collapses N genuinely
    unlogged turns into one, which silences exactly the actor C4 exists to reach. So the
    over-count stays, bounded at one per double-checked turn, until C5 makes turns countable.

    Pure function over rows — no store, no viewer, no I/O. It never sees *whose* rows these
    are, so it cannot leak another viewer's rows; note that it will faithfully count whatever
    list it is handed, so the actor scoping it depends on is enforced twice by its callers
    (the store's predicate and ``ServerContext._coverage_hint``'s re-check — §3.3 の流儀).
    """
    checks: list[bool] = []          # one entry per policy_check: was it followed by a write?
    for row in rows:
        tool = row.get("tool")
        if tool == "policy_check":
            checks.append(False)
        elif tool == "usage_recorded" and checks:
            checks[-1] = True
    if checks and not checks[-1]:
        checks.pop()                 # the in-flight turn — see the docstring
    recent = checks[-window:]
    missing = sum(1 for recorded in recent if not recorded)
    if len(recent) < 2 or not missing:
        return None
    return (
        f"直近{len(recent)}回のポリシー確認のうち{missing}回、記録が残っていません"
        "（log_usage の呼び忘れかもしれません。助言であり、利用は止めません）"
    )
