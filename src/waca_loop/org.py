"""Organization profile (``org.toml``) — every value that differs between adopting organizations.

WACA Loop ships with no organization baked in. The organization's name, its Google Workspace
domains, the unit ids that carry special meaning (everyone / steward / board / owner), the words
that mark a conversation as the organization's work, the sheet-label → unit-id map, any
person_ids pinned for already-linked members, and the organization's own ID column names (treated
as personal data) all live in one TOML file that the adopter writes:

* ``org.example.toml`` (in the repository) is the blank template — every value to fill in reads
  ``CHANGE-ME``.
* ``sample/org.toml`` is a filled-in example for the fictional "サンプル協会".
* The adopter copies the template to ``org.toml`` (git-ignored) or points ``ORG_CONFIG`` at it.

The file is read once per process. Without one, every value falls back to a neutral default
(``org`` / ``steward`` / ``board`` / ``owner``, no domains, no keywords) so the pure-logic code
and the offline test suite run unchanged. Entry points that touch real data — the MCP server and
the registration scripts — call :func:`require_complete` first and refuse to start while the
file is missing or still holds ``CHANGE-ME``, listing exactly what to fix.

Pure logic: stdlib only (``tomllib``), no cloud imports.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

PLACEHOLDER = "CHANGE-ME"
ENV_VAR = "ORG_CONFIG"
DEFAULT_FILENAME = "org.toml"
TEMPLATE_FILENAME = "org.example.toml"
DEFAULT_ORG_NAME = "組織"


class OrgConfigError(RuntimeError):
    """The organization profile is missing, malformed, or still holds ``CHANGE-ME``."""


@dataclass(frozen=True)
class OrgUnits:
    """Unit ids with a fixed meaning in the access rules (§3.3)."""

    org: str = "org"            # every member carries it; ownership by it = visible to all
    steward: str = "steward"    # approves what enters shared layers
    board: str = "board"        # its decisions can never be made org-public
    owner: str = "owner"        # WACA Loop administrators (e.g. wontfix on requests)
    export_admin: frozenset[str] = frozenset()  # may exceed per-call result caps with a reason


@dataclass(frozen=True)
class OrgConfig:
    name: str = DEFAULT_ORG_NAME
    workspace_domains: frozenset[str] = frozenset()
    units: OrgUnits = field(default_factory=OrgUnits)
    org_keywords: tuple[str, ...] = ()
    non_org_signals: tuple[str, ...] = ()
    unit_map: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    person_ids: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    pii_id_columns: tuple[str, ...] = ()   # org-specific member/ID column names (e.g. "会員ID")
    # Regexes for the organization's own member/ID numbers found in free text (masked as
    # [会員番号] by ingest.anonymize). Validated to compile at load time.
    pii_id_value_patterns: tuple[str, ...] = ()
    source: Path | None = None          # the file it came from; None = built-in defaults
    requested: Path | None = None       # what ORG_CONFIG asked for, when that file is absent


def find_org_config() -> tuple[Path | None, Path | None]:
    """Return ``(found, requested_but_missing)``.

    Order: ``ORG_CONFIG`` (if set, it is the only candidate), then ``./org.toml``, then an upward
    search for ``org.toml`` from the package location (the repository root in a checkout).
    """
    override = os.environ.get(ENV_VAR)
    if override:
        path = Path(override).expanduser()
        return (path, None) if path.is_file() else (None, path)
    cwd_candidate = Path.cwd() / DEFAULT_FILENAME
    if cwd_candidate.is_file():
        return cwd_candidate, None
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / DEFAULT_FILENAME
        if candidate.is_file():
            return candidate, None
    return None, None


def _str(table: Mapping[str, Any], key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise OrgConfigError(f"{where} {key} は文字列で書いてください（今の値: {value!r}）")
    return value.strip()


def _str_list(table: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise OrgConfigError(f'{where} {key} は文字列の配列で書いてください（例: ["a", "b"]）')
    return tuple(v.strip() for v in value if v.strip())


def _regex_list(table: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    patterns = _str_list(table, key, where)
    for pattern in patterns:
        if PLACEHOLDER in pattern:
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            raise OrgConfigError(
                f"{where} {key} の正規表現が正しくありません: {pattern!r}（{exc}）"
            ) from exc
    return patterns


def parse_org_config(data: Mapping[str, Any], source: Path | None = None) -> OrgConfig:
    """Build an :class:`OrgConfig` from parsed TOML (also used by tests)."""
    org = data.get("org", {})
    units = data.get("units", {})
    guard = data.get("guard", {})
    unit_map = data.get("unit_map", {})
    person_ids = data.get("person_ids", {})
    pii = data.get("pii", {})
    for label, table in (("[org]", org), ("[units]", units), ("[guard]", guard),
                         ("[unit_map]", unit_map), ("[person_ids]", person_ids), ("[pii]", pii)):
        if not isinstance(table, Mapping):
            raise OrgConfigError(f"{label} はテーブルで書いてください")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in unit_map.items()):
        raise OrgConfigError('[unit_map] は「"シート上の名前" = "ユニットID"」の形で書いてください')
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in person_ids.items()):
        raise OrgConfigError('[person_ids] は「"表示名" = "person_id"」の形で書いてください')
    defaults = OrgUnits()
    return OrgConfig(
        name=_str(org, "name", DEFAULT_ORG_NAME, "[org]") or DEFAULT_ORG_NAME,
        workspace_domains=frozenset(
            d.lower() for d in _str_list(org, "workspace_domains", "[org]")
        ),
        units=OrgUnits(
            org=_str(units, "org", defaults.org, "[units]") or defaults.org,
            steward=_str(units, "steward", defaults.steward, "[units]") or defaults.steward,
            board=_str(units, "board", defaults.board, "[units]") or defaults.board,
            owner=_str(units, "owner", defaults.owner, "[units]") or defaults.owner,
            export_admin=frozenset(_str_list(units, "export_admin", "[units]")),
        ),
        org_keywords=_str_list(guard, "org_keywords", "[guard]"),
        non_org_signals=_str_list(guard, "non_org_signals", "[guard]"),
        unit_map=MappingProxyType(
            {k.strip().lower(): v.strip() for k, v in unit_map.items() if k.strip()}
        ),
        pii_id_columns=tuple(c.lower() for c in _str_list(pii, "id_column_names", "[pii]")),
        pii_id_value_patterns=_regex_list(pii, "id_value_patterns", "[pii]"),
        person_ids=MappingProxyType(
            {k.strip(): v.strip() for k, v in person_ids.items() if k.strip()}
        ),
        source=source,
    )


def load_org_config(path: Path | str | None = None) -> OrgConfig:
    """Read the profile. A missing file yields the neutral defaults (never raises for that)."""
    if path is None:
        found, requested = find_org_config()
    else:
        p = Path(path).expanduser()
        found, requested = (p, None) if p.is_file() else (None, p)
    if found is None:
        return OrgConfig(requested=requested)
    try:
        data = tomllib.loads(found.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise OrgConfigError(f"{found} の書き方に誤りがあります: {exc}") from exc
    return parse_org_config(data, source=found)


def _placeholder_paths(cfg: OrgConfig) -> list[str]:
    hits: list[str] = []
    if PLACEHOLDER in cfg.name:
        hits.append("[org] name")
    if any(PLACEHOLDER in d.upper() for d in cfg.workspace_domains):
        hits.append("[org] workspace_domains")
    u = cfg.units
    for key in ("org", "steward", "board", "owner"):
        if PLACEHOLDER in getattr(u, key):
            hits.append(f"[units] {key}")
    if any(PLACEHOLDER in x for x in u.export_admin):
        hits.append("[units] export_admin")
    if any(PLACEHOLDER in k for k in cfg.org_keywords):
        hits.append("[guard] org_keywords")
    if any(PLACEHOLDER in k for k in cfg.non_org_signals):
        hits.append("[guard] non_org_signals")
    if any(PLACEHOLDER in k.upper() or PLACEHOLDER in v for k, v in cfg.unit_map.items()):
        hits.append("[unit_map]")
    if any(PLACEHOLDER in c.upper() for c in cfg.pii_id_columns):
        hits.append("[pii] id_column_names")
    if any(PLACEHOLDER in p for p in cfg.pii_id_value_patterns):
        hits.append("[pii] id_value_patterns")
    return hits


def problems(cfg: OrgConfig) -> list[str]:
    """Human-readable list of what must be fixed before real use (empty = ready)."""
    if cfg.source is None:
        if cfg.requested is not None:
            return [f"{ENV_VAR} で指定された {cfg.requested} が見つかりません"]
        return [
            f"組織設定ファイル {DEFAULT_FILENAME} が見つかりません。"
            f"{TEMPLATE_FILENAME} をコピーして書き換えるか、{ENV_VAR} でファイルを指定してください"
            f"（試すだけなら {ENV_VAR}=sample/org.toml）"
        ]
    return [f"{cfg.source}: {item} がまだ {PLACEHOLDER} のままです" for item in
            _placeholder_paths(cfg)]


def require_complete(cfg: OrgConfig | None = None) -> OrgConfig:
    """Refuse to proceed while the profile is missing or unfinished. Returns the profile."""
    cfg = cfg or get_org_config()
    found = problems(cfg)
    if found:
        raise OrgConfigError(
            "組織設定（org.toml）を確認してください:\n  - " + "\n  - ".join(found)
        )
    return cfg


def exit_if_incomplete() -> OrgConfig:
    """Entry-point guard: print what to fix and exit (status 1) instead of a traceback."""
    try:
        return require_complete()
    except OrgConfigError as exc:
        raise SystemExit(str(exc)) from None


_active: OrgConfig | None = None


def get_org_config() -> OrgConfig:
    """The process-wide profile (read once)."""
    global _active
    if _active is None:
        _active = load_org_config()
    return _active


def set_org_config(cfg: OrgConfig | None) -> None:
    """Replace the process-wide profile (tests; ``None`` re-reads on next access).

    Note: unit ids are bound at import time by :mod:`waca_loop.retrieval.acl`; changing them
    here affects code that reads :func:`get_org_config` at call time (names, keywords, domains,
    unit_map) but not the already-imported unit constants.
    """
    global _active
    _active = cfg
