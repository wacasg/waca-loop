#!/usr/bin/env python3
"""Sync `<DS_GROUNDING>.persons` (§2.2②) from the authoritative org Google Sheet.

The org spreadsheet is the source of truth for roles (担当理事/部長/委員長…). This reads it and
upserts a **PII-minimal directory** (display_name + unit_path + consent + status) — never raw PII
(no birthdate/contact columns exist in the schema, so this is the 🟡 safe-channel directory, not
the 🔴 raw roster). Idempotent: it deletes only the person_ids
it syncs, then loads, so people not in the sheet (e.g. OAuth-mapped admins) are left untouched.

Auth: reads the sheet via the Sheets API using ADC. Grant the scope once:
    gcloud auth application-default login \
      --scopes=https://www.googleapis.com/auth/spreadsheets.readonly,https://www.googleapis.com/auth/cloud-platform

Usage:
    python scripts/sync_persons_from_sheet.py --sheet-id <ID> --range 'Sheet1!A1:M200' --dry-run
    python scripts/sync_persons_from_sheet.py --sheet-id <ID> --range 'Sheet1!A1:M200'
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from waca_loop.org import get_org_config  # noqa: E402
from waca_loop.retrieval.acl import ORG_UNIT  # noqa: E402


def _org_tables() -> tuple[dict[str, str], dict[str, str]]:
    """シート上の名前 → ユニットID / person_id の対応表（org.toml の [unit_map] / [person_ids]）."""
    cfg = get_org_config()
    return (
        {_norm(k): v for k, v in cfg.unit_map.items()},
        {_norm(k): v for k, v in cfg.person_ids.items()},
    )


# Header labels -> the column that also carries the unit level the role attaches to.
# level: "committee" (col 委員会名), "bu" (col 部名), "bumon" (col 部門名).
ROLE_TO_LEVEL = {
    "担当理事": "bumon", "理事補助": "bumon",
    "部長": "bu", "副部長": "bu", "部付": "bu", "事務局": "bu",
    "委員長": "committee", "副委員長": "committee",
}


def _norm(s: str | None) -> str:
    """NFKC-normalize, casefold, strip, drop all whitespace — for map keys/dedup.

    Lowercasing matters: UNIT_MAP is keyed in lower case ("委員会a"), so without it a unit
    name with ASCII capitals (「委員会A」) would miss its slug and the person would land in the
    org-wide unit only.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).strip().lower()
    return "".join(s.split())


# シート上の名前（正規化済み）→ ユニットID。組織ごとの値なので org.toml の [unit_map] に書く。
# person_id の固定（OAuth で紐づけ済みの人の id を変えない, §5）は [person_ids] に書く。
UNIT_MAP, PERSON_ID_OVERRIDES = _org_tables()


def _person_id(display_name: str) -> str:
    key = _norm(display_name)
    if key in PERSON_ID_OVERRIDES:
        return PERSON_ID_OVERRIDES[key]
    return "p_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _units_for(level: str, bumon: str, bu: str, committee: str) -> list[str]:
    """Build unit_path (leaf → ancestors → org-wide unit) from the cascaded context."""
    slugs: list[str] = []
    if level == "committee" and _norm(committee) in UNIT_MAP:
        slugs.append(UNIT_MAP[_norm(committee)])
    if level in ("committee", "bu") and _norm(bu) in UNIT_MAP:
        slugs.append(UNIT_MAP[_norm(bu)])
    if _norm(bumon) in UNIT_MAP:
        slugs.append(UNIT_MAP[_norm(bumon)])
    slugs.append(ORG_UNIT)
    # de-dup preserving order
    seen: set[str] = set()
    return [s for s in slugs if not (s in seen or seen.add(s))]


def manual_units(unit_path: list[str], sheet_derivable: set[str]) -> list[str]:
    """Units on an existing row that this sheet could never have produced (pure, testable).

    Anything outside ``UNIT_MAP``'s values plus the org-wide unit was granted by hand — role
    units like the steward unit. Those must survive a re-sync.
    """
    return [u for u in (unit_path or []) if u not in sheet_derivable]


def _existing_manual_units(client, settings, sheet_derivable: set[str]) -> dict[str, list[str]]:
    """Read current persons and return {person_id: [hand-granted units]} (live I/O)."""
    sql = f"SELECT person_id, unit_path FROM `{settings.persons_table}`"
    return {
        row["person_id"]: manual_units(list(row["unit_path"] or []), sheet_derivable)
        for row in client.query(sql).result()
    }


# --- 単位マスタ tab (部署コード / 単位名 / 部門・所管 形式) -----------------------------------
#
# A different layout from the role-matrix tab `parse_org_sheet` reads. Two stacked blocks,
# each with its own header row, both keyed by 部署コード / 単位名 / 部門・所管:
#   ① 部署一覧      … 部長・委員長 (may hold several names, "・"-separated)
#   ③ メンバー一覧  … 氏名 + メールアドレス
# Unit slugs come from 単位名 and 部門・所管 through the same UNIT_MAP, after stripping the
# parenthetical qualifiers the master carries (「会員部（第2期）」「部門C（所管）」).

# 「部門・所管」 sometimes holds a placement note rather than a unit — not a vocabulary gap.
_PLACEMENT_NOTES = {_norm(x) for x in ("理事会直轄", "理事会直下")}

# 氏名欄に入りうる非氏名のプレースホルダ。
_NOT_A_NAME = {_norm(x) for x in ("—", "-", "―", "未定", "なし", "TBD")}

_UNIT_NAME_COL = "単位名"
_UNIT_PARENT_COL = "部門・所管"
_NAME_COLS = ("部長・委員長", "氏名", "担当理事")
# 「（副：佐藤花子）」— 括弧の中の実在の氏名。「（代行）」のような注記とは区別する。
_DEPUTY_RE = re.compile(r"[（(]\s*副[：:]\s*([^）)]+)[）)]")


def _strip_paren(s: str) -> str:
    """Drop 全角/半角 parentheticals.

    「会員部（第2期）」→「会員部」／「伊藤一郎（代行）」→「伊藤一郎」。
    """
    return re.sub(r"[（(][^）)]*[）)]", "", s or "").strip()


def _split_names(cell_value: str) -> list[str]:
    """1セル → 氏名のリスト。

    「中村次郎・小林三郎」→ 2名。括弧は既定で落とすので「（代行）」は氏名にならないが、
    「（副：佐藤花子）」だけは中身が実在の人なので拾う（落とすと副担当が名簿から消える）。
    「—」「未定」等のプレースホルダは氏名ではない。
    """
    names = [n.strip() for n in _strip_paren(cell_value).split("・") if n.strip()]
    names += [n.strip() for n in _DEPUTY_RE.findall(cell_value or "") if n.strip()]
    return [n for n in names if _norm(n) not in _NOT_A_NAME]


def parse_unit_master_sheet(grid: list[list[str]]) -> tuple[list[dict], list[str]]:
    """Pure: 単位マスタ grid → (person dicts, unmapped 単位名/部門名). Offline-testable.

    Returns the unmapped labels alongside the people so the caller can REFUSE to sync on a
    vocabulary gap rather than silently filing someone under the org-wide unit only — an unmapped
    unit is how a person ends up visible to everyone and owned by no one (§3.3).
    """
    people: dict[str, dict] = {}
    unmapped: list[str] = []
    cols: dict[str, int] = {}

    for row in grid:
        cells = [(c or "").strip() for c in row]
        if not any(cells):
            continue
        # A header row re-arms the column map; the tab stacks two blocks with their own headers.
        if _norm(_UNIT_NAME_COL) in [_norm(c) for c in cells]:
            cols = {_norm(c): i for i, c in enumerate(cells) if c}
            continue
        if not cols:
            continue

        def cell(name: str, _cols: dict = cols, _cells: list = cells) -> str:
            i = _cols.get(_norm(name))
            return _cells[i] if i is not None and i < len(_cells) else ""

        unit_label = _strip_paren(cell(_UNIT_NAME_COL))
        parent_label = _strip_paren(cell(_UNIT_PARENT_COL))
        if not unit_label:
            continue
        units: list[str] = []
        for label in (unit_label, parent_label):
            if not label:
                continue
            slug = UNIT_MAP.get(_norm(label))
            if slug is None:
                if label not in unmapped and _norm(label) not in _PLACEMENT_NOTES:
                    unmapped.append(label)
                continue
            if slug not in units:
                units.append(slug)
        if not units:
            continue
        units.append(ORG_UNIT)

        email_i = cols.get(_norm("メールアドレス"))
        email = cells[email_i].strip() if email_i is not None and email_i < len(cells) else ""
        for col in _NAME_COLS:
            for name in _split_names(cell(col)):
                rec = people.setdefault(
                    _norm(name),
                    {"display_name": name, "unit_path": [], "email": None, "emails": []},
                )
                for u in units:
                    if u not in rec["unit_path"]:
                        rec["unit_path"].append(u)
                if email:
                    if email not in rec["emails"]:
                        rec["emails"].append(email)
                    if not rec["email"]:
                        rec["email"] = email

    # One name carrying two different addresses is a sheet error, not a person with two
    # mailboxes: rows are merged BY NAME, so the second address is silently dropped and that
    # person gets pre-registered under someone else's address. Report it like an unmapped
    # unit so the caller refuses to sync. (Typical cause: a row copied from a colleague with
    # the email changed but not the 氏名.)
    for rec in people.values():
        if len(rec["emails"]) > 1:
            unmapped.append(
                f"氏名「{rec['display_name']}」に別々のメールアドレスが付いています"
                f"（{'、'.join(rec['emails'])}）— 氏名欄の修正が必要です"
            )
    out = [
        {
            "person_id": _person_id(rec["display_name"]),
            "display_name": rec["display_name"],
            "unit_path": rec["unit_path"],
            "consent_internal": True,
            "consent_external": False,
            "status": "active",
            "_email": rec["email"],
        }
        for rec in people.values()
    ]
    return out, unmapped


def parse_org_sheet(grid: list[list[str]]) -> list[dict]:
    """Pure: sheet grid (incl. header row) → deduped person dicts. Offline-testable.

    Forward-fills 部門名/部名 (blank = inherit above). Each filled role cell yields a person in the
    unit that role attaches to; a person appearing in several rows is merged (units unioned, first
    non-empty email kept).
    """
    if not grid:
        return []
    header = [_norm(h) for h in grid[0]]
    idx = {name: i for i, name in enumerate(header)}

    def cell(row: list[str], name: str) -> str:
        i = idx.get(_norm(name))
        return row[i].strip() if i is not None and i < len(row) and row[i] else ""

    people: dict[str, dict] = {}
    bumon = bu = ""
    for row in grid[1:]:
        bumon = cell(row, "部門名") or bumon
        bu = cell(row, "部名") or bu
        committee = cell(row, "委員会名")
        for role, level in ROLE_TO_LEVEL.items():
            name = cell(row, role)
            if not name:
                continue
            # a role's メールアドレス is the column immediately to its right (the header repeats
            # "メールアドレス", so idx-by-name can't disambiguate — use position).
            email = None
            r_i = idx.get(_norm(role))
            if (
                r_i is not None
                and r_i + 1 < len(header)
                and header[r_i + 1] == _norm("メールアドレス")
                and r_i + 1 < len(row)
                and row[r_i + 1]
            ):
                email = row[r_i + 1].strip()
            units = _units_for(level, bumon, bu, committee)
            key = _norm(name)
            rec = people.setdefault(
                key, {"display_name": name.strip(), "unit_path": [], "email": None}
            )
            for u in units:
                if u not in rec["unit_path"]:
                    rec["unit_path"].append(u)
            if email and not rec["email"]:
                rec["email"] = email
    out = []
    for rec in people.values():
        out.append(
            {
                "person_id": _person_id(rec["display_name"]),
                "display_name": rec["display_name"],
                "unit_path": rec["unit_path"],
                "consent_internal": True,
                "consent_external": False,
                "status": "active",
                "_email": rec["email"],  # not stored in persons; hint for oauth mapping
            }
        )
    return out


def read_grid_file(path: str) -> list[list[str]]:
    """Read the sheet from a local export instead of the API (.tsv / .csv / .json grid).

    The Sheets API path needs an ADC login carrying the spreadsheets scope, and Google blocks
    the Cloud SDK client from requesting it. A
    manual export (シート → ファイル → ダウンロード → TSV) needs no new permission at all, so
    this keeps the sync runnable without a service account.
    """
    import csv
    import json

    text = Path(path).read_text(encoding="utf-8-sig")
    if path.endswith(".json"):
        return [[str(c or "") for c in row] for row in json.loads(text)]
    delimiter = "\t" if path.endswith((".tsv", ".tab")) else ","
    return [list(row) for row in csv.reader(text.splitlines(), delimiter=delimiter)]


def _read_grid_live(
    sheet_id: str, cell_range: str, impersonate: str | None = None
) -> list[list[str]]:
    """Read via the Sheets API.

    Two credential routes, because a plain user ADC cannot carry the spreadsheets scope:
    Google blocks the Cloud SDK client from requesting it ("このアプリはブロックされます").

      - ``impersonate``: mint a service-account token with the Sheets scope via IAM
        impersonation. The SHEET MUST BE SHARED with that service account (閲覧者) — the SA
        is a principal like any other. This needs no browser and no key file; the caller
        needs roles/iam.serviceAccountTokenCreator on the SA.
      - otherwise: whatever ADC already carries (works if the scope was granted some other way)
    """
    from google.auth import default
    from google.auth.transport.requests import AuthorizedSession

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    if impersonate:
        from google.auth import impersonated_credentials

        source, _ = default()
        creds = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=impersonate,
            target_scopes=scopes,
        )
    else:
        creds, _ = default(scopes=scopes)
    sess = AuthorizedSession(creds)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{cell_range}"
    resp = sess.get(url)
    resp.raise_for_status()
    return resp.json().get("values", [])


def upsert_persons(client, settings, rows: list[dict]) -> None:
    """DELETE the given person_ids, then load ``rows`` (live I/O; shared with register_members).

    Only the listed person_ids are touched, so people not in ``rows`` are left as they are.
    """
    from google.cloud import bigquery

    table = client.get_table(settings.persons_table)
    ids = [r["person_id"] for r in rows]
    client.query(
        f"DELETE FROM `{settings.persons_table}` WHERE person_id IN UNNEST(@ids)",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)]
        ),
    ).result()
    client.load_table_from_json(
        rows, table,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND, schema=table.schema
        ),
    ).result()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet-id", help="Sheets API で読む場合（要 spreadsheets スコープ）")
    parser.add_argument("--range", default="A1:M300", dest="cell_range")
    parser.add_argument(
        "--grid-file",
        help="シートを手元に書き出したファイルから読む（.tsv/.csv/.json）。"
             "追加の権限が要らないので、ADC のスコープが取れない環境ではこちら",
    )
    parser.add_argument(
        "--impersonate",
        help="このサービスアカウントとしてシートを読む（例 xxx@yyy.iam.gserviceaccount.com）。"
             "シートを当該SAに閲覧者で共有しておくこと",
    )
    parser.add_argument(
        "--layout", choices=("unit-master", "role-matrix"), default="unit-master",
        help="unit-master: 単位マスタ形式(部署コード/単位名/部門・所管) — 既定。"
             " role-matrix: 役職マトリクス形式(部門名/部名/部長…)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run:
        # 実データに書く前に、組織設定（org.toml）が埋まっているかを確かめる。
        from waca_loop.org import exit_if_incomplete

        exit_if_incomplete()

    if not args.grid_file and not args.sheet_id:
        parser.error("--sheet-id か --grid-file のどちらかが必要です")
    grid = (
        read_grid_file(args.grid_file)
        if args.grid_file
        else _read_grid_live(args.sheet_id, args.cell_range, args.impersonate)
    )
    if args.layout == "unit-master":
        people, unmapped = parse_unit_master_sheet(grid)
        if unmapped:
            # An unmapped 単位名 does not mean "no unit" — it means this person would be filed
            # under the org-wide unit alone, i.e. owned by nobody and visible to everyone
            # (§3.3). Refuse.
            print("ERROR: シートを直してから再実行してください:")
            for label in unmapped:
                print(f"   - {label}")
            return 2
    else:
        people = parse_org_sheet(grid)

    from waca_loop.config import get_settings

    settings = get_settings()

    if args.dry_run:
        print(
            f"-- parsed {len(people)} persons from sheet "
            f"(would upsert into {settings.persons_table}):"
        )
        for p in people:
            mail = f"  <{p['_email']}>" if p["_email"] else ""
            print(f"   {p['person_id']:20s} {p['display_name']:10s} {p['unit_path']}{mail}")
        return 0

    if not settings.gcp_project:
        print("ERROR: GCP_PROJECT required. Set it in .env.", file=sys.stderr)
        return 2

    from datetime import UTC, datetime

    from waca_loop.bq import make_client

    client = make_client(settings)
    now = datetime.now(UTC).isoformat()
    # The sheet cannot express role grants — the steward unit (governance authority; gates every
    # promotion AND is where an un-routable incident report lands) and any export-admin unit
    # exist only as manual grants. This sync is DELETE + APPEND, so without this it silently
    # strips them: the organization would end up with zero stewards and no way to approve
    # anything. Carry forward every unit the sheet could not have produced.
    sheet_derivable = set(UNIT_MAP.values()) | {ORG_UNIT}
    preserved = _existing_manual_units(client, settings, sheet_derivable)

    rows = []
    for p in people:
        units = list(p["unit_path"])
        for u in preserved.get(p["person_id"], []):
            if u not in units:
                units.append(u)
        rows.append({
            "person_id": p["person_id"], "display_name": p["display_name"],
            "unit_path": units, "consent_internal": p["consent_internal"],
            "consent_external": p["consent_external"], "circles": [],
            "status": p["status"], "created_at": now, "updated_at": now,
        })
    kept = {pid: us for pid, us in preserved.items() if us}
    if kept:
        print(f"   preserving manual unit grants (not in the sheet): {kept}")
    upsert_persons(client, settings, rows)
    print(f"synced {len(rows)} persons from sheet into {settings.persons_table}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
