"""WACA Loop ops guards (SPEC_ops P0) — session scope, category normalization, org-context gate.

Pure stdlib module (INV-2): no BigQuery/MCP imports. These are the *server-side* half of the
two-line defense (G4): tool descriptions instruct the model, and these guards enforce what the
description alone cannot guarantee (descriptions are advisory; models skip them probabilistically
— SPEC_ops §1).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime

# --- R8: forgiving use_category normalization ------------------------------------------------

VALID_USE_CATEGORIES = ("l2", "l1", "l0", "prohibited")  # 軽い順

_CATEGORY_ALIASES = {
    # 正典語彙（L0/L1/L2＋禁止列挙）
    "l0": "l0", "l1": "l1", "l2": "l2", "prohibited": "prohibited",
    "禁止": "prohibited", "禁止列挙": "prohibited", "下限": "prohibited", "floor": "prohibited",
    # 裸の数字（"0"/"1"/"2"）は受けない: 正典の L0=最重 と classify の severity 0=最軽が
    # 逆向きで、取り違えると申告が黙って重く/軽くなる。
    # 三色表記 — vocab-definition-site (docs/HOOKS_SPEC.md G2)。1:1 で写す（🟢→L2 / 🟡→L1 /
    # 🔴→L0）。三色には prohibited に当たる語が無いため、下限が発火する申告に red を使うと
    # mismatch=True が付く。助言のみで記録は常に成功する。
    "red": "l0", "yellow": "l1", "green": "l2",
    "🔴": "l0", "🟡": "l1", "🟢": "l2",
    "赤": "l0", "黄": "l1", "緑": "l2",
    "r": "l0", "y": "l1", "g": "l2",
}


def normalize_use_category(raw: str) -> str:
    """Map a caller-supplied category to l0|l1|l2|prohibited, forgiving variants (R8).

    Models see the valid values in the tool description, but callers still send emoji /
    Japanese / case variants; rejecting those would lose real ledger rows. 三色 tokens are
    accepted too, so a client using a 三色-style description keeps recording instead of
    erroring.
    Raises ``ValueError`` naming the valid values when the input maps to nothing.
    """
    key = (raw or "").strip().lower()
    normalized = _CATEGORY_ALIASES.get(key)
    if normalized is None:
        raise ValueError(
            f"invalid use_category {raw!r}: must be one of {'|'.join(VALID_USE_CATEGORIES)} "
            "(red/yellow/green, 🔴/🟡/🟢 and 赤/黄/緑 are also accepted)"
        )
    return normalized


# --- R1: session scope (org / external / private) -------------------------------------------

_log = logging.getLogger(__name__)

VALID_SCOPES = ("org", "external", "private", "unknown")

# Scopes whose sessions must never write to the organization's ledger/knowledge layers.
EXCLUDED_SCOPES = frozenset({"external", "private"})

#: The §5 identity a declaration is filed under: (actor_id, actor_raw). Exactly one is set.
ScopeKey = tuple


def scope_key(actor_id: str | None, actor_raw: str | None) -> ScopeKey:
    """Normalize an actor into the one key both the registry and the store file under.

    Defined **here** rather than in the store so the pure layer owns the definition and the
    I/O layer imports it — the reverse would point ``guard`` at ``ledger`` (INV-2). Without a
    single definition the two would drift: if the store normalized (``actor_id`` wins) while the
    registry keyed on the raw tuple, ``declare(actor_id="p", actor_raw="h")`` and
    ``scope_of(actor_id="p")`` would become two independent declarations.
    """
    # Falsy, not `is not None`: an empty string would otherwise form a bucket shared by every
    # empty-subject caller, and `BigQuerySessionScopeStore.get` matches `''` happily.
    # `tool_events.same_actor` defends against exactly this; under R1d the misfire would be a
    # *permanent* cross-actor block plus a `stored_scope` disclosure, not a 12h one.
    # Unreachable in practice — hosted mode refuses a null person_id — which is why it is a
    # guard rather than a fix.
    if actor_id:
        return (actor_id, None)
    return (None, actor_raw) if actor_raw else (None, None)

#: How long a declaration stands before it expires back to ``unknown`` (SPEC_ops R1).
#: Overridden per-deployment by ``Settings.scope_ttl_hours``; this is the offline default.
DEFAULT_SCOPE_TTL_HOURS = 12

#: Warn this long before expiry, so re-declaring is a choice rather than a surprise.
SCOPE_EXPIRY_WARNING_HOURS = 2


def scope_expired(
    declared_at: datetime | None, ttl_hours: int = DEFAULT_SCOPE_TTL_HOURS
) -> bool:
    """True when a declaration is old enough to have lapsed.

    A declaration with **no timestamp** never expires: a row stored without a timestamp,
    or a duck-typed store that does not keep one, and silently treating it as lapsed
    would resume recording for someone who declared 業務外 and was never told. Unknown age
    fails toward keeping the exclusion.

    ``ttl_hours <= 0`` disables expiry entirely (for a deployment that wants declarations to
    stand until they are re-declared, without a code change).
    """
    if declared_at is None or ttl_hours <= 0:
        return False
    if declared_at.tzinfo is None:
        return False
    return (datetime.now(UTC) - declared_at).total_seconds() > ttl_hours * 3600


def scope_expires_in_hours(
    declared_at: datetime | None, ttl_hours: int = DEFAULT_SCOPE_TTL_HOURS
) -> float | None:
    """Hours left before the declaration lapses; ``None`` when it never will."""
    if declared_at is None or ttl_hours <= 0 or declared_at.tzinfo is None:
        return None
    elapsed = (datetime.now(UTC) - declared_at).total_seconds()
    return (ttl_hours * 3600 - elapsed) / 3600

# 文言の要点は2つ。宣言は **セッションではなく subject に紐づいて常駐する**ので「このセッション」
# とは書かない。そして**解除の方法を必ず書く** — 書かないと、過去の宣言を保持したまま数セッション
# にわたり記録が拒否され続け、拒否文を読んでも原因も戻し方も分からない。宣言そのものは
# fail-closed のまま変えない。
WRITE_REFUSAL_TEMPLATE = (
    "waca-loop への記録を見送りました。この利用者は以前 scope={scope} を宣言しており、"
    "**宣言はセッションをまたいで残ります**{age}。"
    "いまが 組織の業務なら set_session_scope に scope=\"org\" を渡して宣言し直してください"
    "（そのうえで再度記録すれば残ります）。組織の業務外ならこのままで正しい挙動です。"
)


#: After a lapse, recording does **not** resume on its own (SPEC_ops R1d). If the TTL
#: unblocked writes, a conversation that was still 業務外 when the window closed would have its
#: next turn recorded — into a ledger whose only deletion path (R6) is a *soft* delete the owner
#: has to notice and ask for. So the TTL *prompts* instead of *resuming*: it ends the standing
#: declaration's authority but keeps writes refused until the actor says which way it goes. R6
#: shortens the tail of the mistake; it does not make the mistake cheap, because the person has
#: to spot the row first.
#: No ``{age}`` here, unlike :data:`WRITE_REFUSAL_TEMPLATE`. The lapsed state is unbounded in
#: time, and interpolating it would put 「この利用者は N 日前に private を宣言した」 in front of
#: the model on *every* turn until re-declared — a line that ends up in bug reports and shared
#: transcripts. It is also redundant next to 「自動失効しています」, which already says "older
#: than the TTL".
LAPSED_REFUSAL_TEMPLATE = (
    "waca-loop への記録を見送りました。この利用者の scope={scope} の宣言は"
    "**自動失効しています**が、**失効しても記録は自動では再開しません**。"
    "いまが 組織の業務なら set_session_scope に scope=\"org\" を渡して宣言し直して"
    "ください（そのうえで再度記録すれば残ります）。まだ業務外なら、同じ scope を"
    "宣言し直せば期限が延びます。"
)


def lapsed_refusal_message(scope: str, declared_at: datetime | None = None) -> str:
    """Refusal text for an exclusion that has expired but not been replaced (R1d).

    Deliberately different prose from :func:`write_refusal_message`: the two states need
    different actions from the reader. A *standing* exclusion is working as intended and the
    only question is whether it should be lifted; a *lapsed* one is a question that has gone
    unanswered, and either answer — back to work, or still 業務外 — needs one explicit call.
    """
    del declared_at   # kept in the signature for symmetry; deliberately not interpolated
    return LAPSED_REFUSAL_TEMPLATE.format(scope=scope)


def write_refusal_message(
    scope: str,
    declared_at: datetime | None = None,
    expires_in_hours: float | None = None,
) -> str:
    """Refusal text naming the standing declaration and how to undo it.

    ``declared_at`` が R1 の「件数のみ・内容は持たない」に反しないのは、時刻が内容でないから
    というより、**同じ情報を系がすでに永続化しているから**である: `set_session_scope` は
    `_trail("scope_declared", detail=<scope>)` を書き、`sql/ddl/11_tool_events.sql` が
    occurred_at + actor + detail を BigQuery に残す。ここで持つのはその真部分集合にすぎない。
    古い宣言ほど「前のセッションの宣言が残っている」可能性が高く、それが読み手に要る情報。
    """
    return WRITE_REFUSAL_TEMPLATE.format(
        scope=scope, age=_age_phrase(declared_at) + _expiry_phrase(expires_in_hours)
    )


def _age_phrase(declared_at: datetime | None) -> str:
    """How long the declaration has been standing, in the unit that reads best.

    Days past 24h: the case this exists for is a declaration left over from another day, and
    「約216時間経過」 is the least legible way to say 「9日前」. Negative deltas (clock skew,
    a stamp from a restarted process) fall back to saying nothing rather than asserting
    「1時間以内」 — an age we cannot compute should not be reported as a small one.
    """
    if declared_at is None or declared_at.tzinfo is None:
        # A naive datetime can only arrive by direct mutation or a duck-typed registry; saying
        # nothing beats raising inside a message whose whole job is to explain a refusal.
        return ""
    hours = (datetime.now(UTC) - declared_at).total_seconds() / 3600
    if hours < 0:
        return ""
    if hours >= 24:
        return (
            f"（宣言から約{int(hours // 24)}日経過 — "
            "別のセッションでの宣言が残っている公算が大）"
        )
    if hours >= 1:
        return f"（宣言から約{int(hours)}時間経過）"
    return "（宣言は1時間以内）"


@dataclass
class SessionScopeRegistry:
    """Session-scope declarations, keyed by the §5 identity pair (R1).

    **Keyed by the actor, not the raw subject.** The declaration is persisted, so the raw
    OAuth ``sub`` must never be a stored key (§5). Resolved
    viewers key on ``actor_id``, unresolved on the already-hashed ``actor_raw`` — the same split
    the ledger uses, so "same actor" means one thing across the system.

    **Two things end a declaration, and only two**: declaring again, or the TTL. A memory-only
    registry would make restart a third, silent one — every deploy would reset everyone to
    ``unknown`` and a ``private`` conversation could drift back to being recorded with nobody
    doing anything. ``store`` removes that: declarations survive restarts, and expiry is a
    single predictable rule.

    ``reason_summary`` is still never stored, in memory or in the store: exclusion metrics keep
    counts only, never content or org names (R1 acceptance).
    """

    #: Persisted backing store (InMemory/BigQuerySessionScopeStore). ``None`` means
    #: memory-only — stdio and tests, where surviving a restart is meaningless.
    store: object | None = None
    ttl_hours: int = DEFAULT_SCOPE_TTL_HOURS
    #: How long a cached read stays authoritative. Not a performance knob: without it an
    #: entry loaded once is authoritative for the life of the process, so an operator's manual
    #: row edit (the only way to remove a *scope* row — R6 withdraws `usage_log` rows, not
    #: these) has no effect until restart, and a second instance would keep recording someone
    #: who declared elsewhere.
    cache_seconds: int = 300

    # repr=False: a stray `repr(ctx)` in a future debug line or a locals-capturing error
    # reporter would otherwise dump every actor in the org with what they declared and when.
    _scopes: dict[ScopeKey, str] = field(default_factory=dict, repr=False)
    _declared_at: dict[ScopeKey, datetime] = field(default_factory=dict, repr=False)
    _loaded_at: dict[ScopeKey, datetime] = field(default_factory=dict, repr=False)
    #: Actors already announced as lapsed, so the notice fires once rather than every turn.
    _lapse_announced: set = field(default_factory=set, repr=False)
    #: Actors whose last store read failed and left no cached answer (R1d). Their writes
    #: go to `pending` rather than being recorded on a guess.
    _unverified: set = field(default_factory=set, repr=False)
    #: Blocking rows that disappeared from the store (retention), pending a one-shot notice.
    _vanished: dict = field(default_factory=dict, repr=False)
    excluded_declarations: int = 0   # count-only metric (R1)
    #: Store failures, counted. A climbing number is the ONLY signal that persistence is not
    #: working — including the case where `sql/ddl/13_session_scopes.sql` was never applied,
    #: which is otherwise indistinguishable from a healthy deployment.
    store_write_failures: int = 0
    store_read_failures: int = 0

    def declare(self, *, actor_id: str | None, actor_raw: str | None, scope: str) -> str:
        if scope not in VALID_SCOPES:
            raise ValueError(f"invalid scope {scope!r}: must be one of {'|'.join(VALID_SCOPES)}")
        key = scope_key(actor_id, actor_raw)
        if scope == "unknown" and self.is_lapsed(actor_id=actor_id, actor_raw=actor_raw):
            # R1d asks for an *explicit* answer before recording resumes, and `unknown` is not
            # one — it is the value a model sends when it cannot tell, which is exactly the
            # state the lapse is asking about. Accepting it would make "I don't know" a way to
            # reopen the ledger.
            raise ValueError(
                "失効した除外の宣言は scope=\"unknown\" では解除できません。"
                "組織の業務に戻ったなら \"org\" を、まだ業務外なら同じ scope を"
                "宣言し直してください。"
            )
        now = datetime.now(UTC)
        self._scopes[key] = scope
        self._declared_at[key] = now
        self._loaded_at[key] = now
        self._lapse_announced.discard(key)
        if scope in EXCLUDED_SCOPES:
            self.excluded_declarations += 1
        if self.store is not None and key != (None, None):
            # Best-effort **for the caller**, but never silent: the in-memory copy already took
            # effect, so failing the call would tell someone they are NOT excluded when in this
            # process they are. What a failure does cost is the restart-survival the store
            # exists for, so it is logged and counted.
            try:
                self.store.upsert(
                    actor_id=key[0], actor_raw=key[1], scope=scope, declared_at=now
                )
            except Exception as exc:   # noqa: BLE001 — see above
                self.store_write_failures += 1
                _log.warning(
                    "session scope not persisted (scope=%s); it will be lost on restart: %s",
                    scope, exc,
                )
        return scope

    def _load(self, actor_id: str | None, actor_raw: str | None) -> None:
        """Refresh the in-process cache from the store, honouring ``cache_seconds``."""
        key = scope_key(actor_id, actor_raw)
        if self.store is None or key == (None, None):
            return
        loaded = self._loaded_at.get(key)
        if loaded is not None and (datetime.now(UTC) - loaded).total_seconds() < self.cache_seconds:
            return
        try:
            found = self.store.get(actor_id=key[0], actor_raw=key[1])
        except Exception as exc:   # noqa: BLE001
            # **This degrades to fail-OPEN and there is no honest way around it here**: we do
            # not know whether the actor had a declaration, and refusing every write for
            # everyone on a transient 503 is worse than the alternative. So it is counted and
            # logged loudly, the miss is NOT cached (the next call retries), and the write path
            # falls back to R1b's per-item content gate — which is a heuristic, not the
            # exclusion.
            self.store_read_failures += 1
            # SPEC_ops R1d: the caller can ask about this via `scope_unverified` and record
            # as `pending` instead of guessing, so "we could not read" is not confused with
            # "nothing declared".
            self._unverified.add(key)
            _log.warning("session scope unreadable — writes for this actor go to pending: %s", exc)
            return
        self._unverified.discard(key)
        self._loaded_at[key] = datetime.now(UTC)
        if found is None:
            # The row is gone where one used to block. Retention does this: `session_scopes` is
            # PARTITION BY DATE(declared_at) with partition_expiration_days = 90, so a
            # declaration nobody renewed disappears on day 91 and the block silently lifts —
            # a second, slower resumption path that R1d says must not exist. Retention stays
            # (the row reads as a label stuck on a person); the disappearance announces itself.
            if self._scopes.get(key) in EXCLUDED_SCOPES:
                self._vanished[key] = self._scopes[key]
            # Cache the miss too. Without it every turn of every user who never declared —
            # nearly everyone — would cost a BigQuery job, which is both latency and one more
            # chance to hit the read failure above.
            self._scopes.pop(key, None)
            self._declared_at.pop(key, None)
            return
        scope, declared_at = found
        previous = self._declared_at.get(key)
        if previous is None or (declared_at is not None and declared_at > previous):
            # A declaration that arrived through the store rather than through this process —
            # another instance, or an operator editing the row by hand, which is the ONLY way to
            # remove a scope row (R6 covers `usage_log`, not this table). Without this the
            # one-shot lapse notice stays permanently disarmed for that actor, and under R1d
            # that notice is what keeps an unbounded block from being silent.
            self._lapse_announced.discard(key)
        self._scopes[key] = scope
        self._declared_at[key] = declared_at

    def scope_of(self, *, actor_id: str | None, actor_raw: str | None) -> str:
        """The standing scope, or ``unknown`` — including when the declaration has lapsed."""
        self._load(actor_id, actor_raw)
        key = scope_key(actor_id, actor_raw)
        scope = self._scopes.get(key, "unknown")
        if scope != "unknown" and scope_expired(self._declared_at.get(key), self.ttl_hours):
            return "unknown"
        return scope

    def take_lapse_notice(self, *, actor_id: str | None, actor_raw: str | None) -> str | None:
        """Once, when an exclusion has just lapsed: the scope it used to be.

        Under SPEC_ops R1d nothing resumes here: the lapse ends the declaration's
        **authority**, and writes stay refused until the actor declares again. What the notice
        does is keep that from being silent — a block that outlives its declaration and never
        says so is indistinguishable from a bug, and the block is unbounded in time.

        Deliberately *not* a sliding TTL: a refused write cannot tell "still 業務外" from
        "戻し忘れ", so it is not evidence of
        either, and refreshing on it would assert one of them.
        """
        key = scope_key(actor_id, actor_raw)
        was = self._scopes.get(key)
        if was is None or was not in EXCLUDED_SCOPES or key in self._lapse_announced:
            return None
        if not scope_expired(self._declared_at.get(key), self.ttl_hours):
            return None
        self._lapse_announced.add(key)
        return was

    def declared_at(
        self, *, actor_id: str | None, actor_raw: str | None
    ) -> datetime | None:
        """When this actor last declared, or ``None`` if never."""
        self._load(actor_id, actor_raw)
        return self._declared_at.get(scope_key(actor_id, actor_raw))

    def expires_in_hours(
        self, *, actor_id: str | None, actor_raw: str | None
    ) -> float | None:
        """Hours before the standing declaration lapses; ``None`` when it never will."""
        return scope_expires_in_hours(
            self.declared_at(actor_id=actor_id, actor_raw=actor_raw), self.ttl_hours
        )

    def stored_scope(self, *, actor_id: str | None, actor_raw: str | None) -> str:
        """What the actor last declared, **ignoring expiry** — ``unknown`` if never.

        :meth:`scope_of` answers "what is in force"; this answers "what did they say". The two
        diverge exactly in the lapsed window, and every message shown to a human wants this
        one: telling someone their scope is ``unknown`` when they declared ``private`` this
        morning is true of the system and false to them.
        """
        self._load(actor_id, actor_raw)
        return self._scopes.get(scope_key(actor_id, actor_raw), "unknown")

    def is_lapsed(self, *, actor_id: str | None, actor_raw: str | None) -> bool:
        """An exclusion whose TTL has passed and which no later declaration has replaced.

        This is the state SPEC_ops R1d defines. If expiry silently handed the session back
        to recording, the TTL's cheap direction (a forgotten declaration stops swallowing
        records) would be bought with its expensive one (a conversation that is *still* 業務外
        gets written down, and R6 only takes it back if the person notices the row and asks).

        So expiry ends the declaration's *authority* without granting the opposite: writes
        stay refused until the actor declares again, either way. Re-declaring clears this for
        free — the store is append-only and read-latest, so a new row moves ``declared_at``
        and the predicate goes false without any state to reset.
        """
        self._load(actor_id, actor_raw)
        key = scope_key(actor_id, actor_raw)
        scope = self._scopes.get(key)
        if scope is None or scope not in EXCLUDED_SCOPES:
            return False
        return scope_expired(self._declared_at.get(key), self.ttl_hours)

    def scope_unverified(self, *, actor_id: str | None, actor_raw: str | None) -> bool:
        """The store could not be read and nothing cached answers for this actor (R1d).

        Distinct from "nothing declared": this is the state where the honest answer is *we do
        not know*, and R1d routes it to `pending` rather than to a guess in either direction.
        """
        self._load(actor_id, actor_raw)
        return scope_key(actor_id, actor_raw) in self._unverified

    def take_vanished_notice(self, *, actor_id: str | None, actor_raw: str | None) -> str | None:
        """Once, when a blocking row disappeared from the store: the scope it used to be."""
        self._load(actor_id, actor_raw)
        return self._vanished.pop(scope_key(actor_id, actor_raw), None)

    def writes_blocked(self, *, actor_id: str | None, actor_raw: str | None) -> bool:
        """True while an exclusion stands **and** after it lapses, until re-declared (R1d)."""
        if self.scope_of(actor_id=actor_id, actor_raw=actor_raw) in EXCLUDED_SCOPES:
            return True
        return self.is_lapsed(actor_id=actor_id, actor_raw=actor_raw)


# --- R2: lightweight org-context gate --------------------------------------------------------

# The words that mark a usage as the organization's work come from org.toml
# ([guard] org_keywords) and are read at call time. Matching ANY keyword marks the usage as
# organization-related. The gate is advisory-grade (needs_confirmation → human confirms), so
# false negatives are cheap and false positives (blocking real work) are what we tune against.
# With no keywords configured the gate does not judge at all: everything records (R2 off).


def _org_keywords() -> tuple[str, ...]:
    from waca_loop.org import get_org_config

    return tuple(k.lower() for k in get_org_config().org_keywords)


def _non_org_signals() -> tuple[str, ...]:
    from waca_loop.org import get_org_config

    return tuple(k.lower() for k in get_org_config().non_org_signals)


NEEDS_CONFIRMATION_MESSAGE = (
    "組織の業務との関連が確認できないため記録を保留しました。組織の業務であれば org_context に"
    "関連(部署・案件など)を書いて再送するか、業務外なら set_session_scope で external/private "
    "を宣言してください(**その宣言は会話をまたいで残る** — 組織の業務に戻ったら org で宣言し"
    "直すこと)。迷う場合はユーザーに記録可否を確認してください。"
)


#: Why a row is `pending` when the scope store could not be read (SPEC_ops R1d).
#: Deliberately different prose from :data:`NEEDS_CONFIRMATION_MESSAGE`: that one says "this
#: looks unrelated to the organization", which sends the reader to inspect their own summary.
#: This one is about the system, and the action it asks for is a declaration, not a rewrite.
SCOPE_UNVERIFIED_MESSAGE = (
    "この利用者が業務外を宣言していないかを確認できなかったため、記録を保留しました"
    "（記録そのものは残っています）。組織の業務であれば set_session_scope に "
    "scope=\"org\" を渡して宣言してください。業務外なら external/private を宣言して"
    "ください。保留は後からスチュワードが承認または削除します。"
)


#: The ONLY thing a caller learns when the ledger write raises.
#: Fixed prose plus a stable code, on purpose: the exception body can carry the row that
#: failed — summary text, the actor, a BigQuery error echoing the payload — and a tool error
#: is prose the client shows to the user and may itself log. So the outside gets a code and
#: an instruction, and the diagnosis stays in the server's own log as a class name.
#: The instruction matters: resending with the SAME request_id is what makes the retry
#: idempotent, so this sentence and the de-duplication are one mechanism.
#: What a ``request_id`` may look like once it reaches the ledger (T6 側口).
#: Clients are *instructed* to build and resend a ``request_id``, so the field gets
#: populated — and a client that writes prose into it
#: (``"要約: …"``) lands verbatim text in a table whose only content column is ``summary``.
#: The ledger's summary-only rule is about content reaching the table, not about the name of
#: the column it arrives in. Unrecognised shapes are dropped, not rejected: the record itself
#: matters more than its correlation key (R8 の流儀).
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")


def normalize_request_id(value: str | None) -> str | None:
    """An id-shaped ``request_id``, or ``None`` — never free prose (§4.2)."""
    if not value:
        return None
    candidate = value.strip()
    return candidate if _REQUEST_ID_RE.match(candidate) else None


LEDGER_WRITE_FAILED_CODE = "WACA_LOOP_LEDGER_WRITE_FAILED"
LEDGER_WRITE_FAILED_MESSAGE = (
    f"{LEDGER_WRITE_FAILED_CODE}: 台帳への書き込みに失敗しました。"
    "同じ request_id で再送してください"
)


def has_org_signal(*texts: str | None) -> bool:
    """True when any supplied text contains one of the configured org keywords."""
    keywords = _org_keywords()
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        if any(k in lowered for k in keywords):
            return True
    return False


def is_org_related(*texts: str | None) -> bool:
    """True when the usage counts as the organization's work for the R2 gate.

    No keywords configured (org.toml [guard] org_keywords = []) means the gate is off and
    everything counts; otherwise any keyword match does (case-insensitive).
    """
    if not _org_keywords():
        return True
    return has_org_signal(*texts)


# --- R9: data_kinds vocabulary normalization -------------------------------------------------

# 正典の列挙 (MCP 出力規約 1 / §2.2③). ``none`` は「該当なし」の明示。
VALID_DATA_KINDS = (
    "pii",
    "confidential",
    "internal",
    "public",
    "core_asset",
    "final_judgment",
    "none",
)

# `use_category` は不正値を明確に拒否するが、`data_kinds` も同様に正規化しないと、"PII" や
# 「個人情報」と書かれた瞬間に FLOOR-01 の検知が**無言で外れる**。語彙はポリシー行の
# trigger_kinds まで広げて受ける。
#
# Without this, free-text kinds would reach BigQuery (e.g. ``internal_docs`` / ``source_code``
# / 「用語・社内語（個人情報なし）」) — an enum documented in the tool description only is not
# enforced. Same 流儀 as R8: forgive and map, never reject, because rejecting loses real
# ledger rows.
_DATA_KIND_ALIASES = {
    **{k: k for k in VALID_DATA_KINDS},
    # pii
    "personal": "pii", "personal_information": "pii", "personal_data": "pii",
    "個人情報": "pii", "個人データ": "pii",
    # confidential
    "secret": "confidential", "機密": "confidential", "秘密": "confidential",
    "confidential_info": "confidential", "nda": "confidential",
    # internal
    "internal_docs": "internal", "internal_doc": "internal", "internal_data": "internal",
    "source_code": "internal", "code": "internal", "社内": "internal", "内部": "internal",
    "社内資料": "internal", "業務情報": "internal",
    # public
    "public_web": "public", "open": "public", "公開": "public", "公開情報": "public",
    # core_asset
    "core": "core_asset", "コア資産": "core_asset", "core_assets": "core_asset",
    # final_judgment
    "final_decision": "final_judgment", "最終判断": "final_judgment",
    "judgment": "final_judgment",
    # none
    "": "none", "n/a": "none", "na": "none", "なし": "none", "該当なし": "none",
}

# Substring fallbacks for free-text values, heaviest first — an unrecognised string that
# *mentions* personal information must not be filed as a bland ``internal`` (INV: the ledger's
# sensitivity axis is what floor-warnings key off, §9.4). 「個人情報なし」 is checked before
# 「個人情報」 so a value like 「用語（個人情報なし）」 doesn't invert its own meaning.
_DATA_KIND_SUBSTRINGS = (
    ("個人情報なし", "internal"),
    ("個人情報を含まない", "internal"),
    ("個人情報", "pii"),
    ("pii", "pii"),
    ("氏名", "pii"),
    ("メールアドレス", "pii"),
    ("機密", "confidential"),
    ("confidential", "confidential"),
    ("コア資産", "core_asset"),
    ("core_asset", "core_asset"),
    ("最終判断", "final_judgment"),
    ("公開", "public"),
    ("public", "public"),
)

# What an unmapped value becomes. ``internal`` (not ``none``) so an unknown kind never reads as
# "nothing sensitive here" — the conservative direction for a governance ledger.
_DATA_KIND_FALLBACK = "internal"


def normalize_data_kinds(
    raw: object, allowed_extra: Collection[str] | None = None
) -> tuple[list[str], list[str]]:
    """Map caller-supplied data_kinds onto the ledger's vocabulary (R9).

    The vocabulary is :data:`VALID_DATA_KINDS` **∪ ``allowed_extra``** — the latter carries the
    ``trigger_kinds`` of the applicable policy rows, which are a legitimate second half of this
    axis (§9.4: a rule fires on the kinds it declares, e.g. ``raw_export``). Folding those into
    ``internal`` would silently stop policy rows from firing, so they pass through untouched.

    Returns ``(kinds, unmapped)``: the normalized list (order-preserving, de-duplicated, never
    empty) and the original strings that matched neither vocabulary nor alias — the caller
    surfaces those as advisory output so the model can correct itself, like ``mismatch`` (§9.4).

    Never raises and never drops sensitivity: a value that mentions 個人情報/機密 lands on the
    matching canonical kind rather than the fallback.
    """
    if isinstance(raw, str):  # a caller sending a bare string means one kind, not N characters
        raw = [raw]
    extra = frozenset(allowed_extra or ())
    kinds: list[str] = []
    unmapped: list[str] = []
    for item in raw or []:
        text = str(item).strip()
        # 区切りの違いを畳んでから引く。`personal-information` / `final judgment` のような
        # 書き方は、モデルの出力として十分ありうる。畳まないと別名表に当たらず fallback の
        # `internal` に落ち、**PII や最終判断の申告が「ただの社内情報」に化ける**
        # （下限の検知が弱くなる向きの欠落になる）。
        key = text.lower().replace(" ", "_").replace("-", "_")
        mapped = text if text in extra else _DATA_KIND_ALIASES.get(key)
        if mapped is None:
            lowered = text.lower()
            mapped = next(
                (canon for needle, canon in _DATA_KIND_SUBSTRINGS if needle in lowered),
                _DATA_KIND_FALLBACK,
            )
            if text:
                unmapped.append(text)
        if mapped not in kinds:
            kinds.append(mapped)
    if not kinds:
        kinds = ["none"]
    return kinds, unmapped


# --- R1b: ask before writing a summary that looks like it isn't the org's---------------------

# Signals that the work belongs to *another* affiliation (org.toml [guard] non_org_signals,
# e.g. 大学 / 私事 / 確定申告). Keep them narrow: they only fire the confirmation prompt, and a
# false positive costs one question while a false negative writes someone else's business into
# the organization's ledger. Words that also belong to the organization's own work should go
# in org_keywords instead (an org signal always wins).

# 「組織の業務なら set_session_scope(\"org\") を宣言してから再送」とは案内しない。R1b には
# scope 免除が無いので**その手順ではループが終わらない**。この関門が
# 訊いているのは「この人は何をしている人か」ではなく「**この1件の中身**を組織の台帳に入れて
# よいか」なので、答えになるのは人単位の宣言ではなく、その1件が組織の業務である証拠の側。
SCOPE_CONFIRMATION_MESSAGE = (
    "この内容は他組織・私事の可能性があるため記録していません。ユーザーに『この内容を組織の"
    "WACA Loopに記録してよいか』を確認してください。組織の業務なら org_context に組織との"
    "関連(部署・案件など)を書くか、policy_row_ids に該当行を渡して再送してください"
    "(この関門は1件ごとの中身を見るので、set_session_scope では解除できません)。"
    "業務外なら set_session_scope で external/private を宣言してください"
    "(**業務外の宣言は会話をまたいで残る** — 組織の業務に戻ったら org で宣言し直すこと)。"
)


def looks_non_org(*texts: str | None) -> bool:
    """True when a text carries an explicit other-affiliation signal and no org signal (R1b).

    The organization wins ties: 「大学向けの団体研修」 is the organization's work when 研修 is
    one of its keywords, even though it mentions a university.
    """
    if has_org_signal(*texts):
        return False
    signals = _non_org_signals()
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        if any(s in lowered for s in signals):
            return True
    return False


def _expiry_phrase(expires_in_hours: float | None) -> str:
    """How much of the declaration is left, when there is a TTL to say it about.

    Said at the refusal because that is where someone is already asking "why is this not
    recording" — the same breath should answer "and for how much longer".
    """
    if expires_in_hours is None:
        return "（自動失効なし — 宣言し直すまで残ります）"
    if expires_in_hours <= 0:
        # Unreachable: under R1d a lapsed declaration *is* still refusing. `_write_refusal`
        # splits lapsed from standing and only the standing branch reaches here.
        return ""
    if expires_in_hours < 1:
        return "（あと1時間以内に自動失効します）"
    return f"（あと約{int(expires_in_hours)}時間で自動失効します）"
