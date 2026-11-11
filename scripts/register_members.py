#!/usr/bin/env python3
"""名簿ファイル（JSON）からメンバーを登録する — 組織シートを使わない組織向け（§5）。

1回の実行で次の2つを行う:
  1. `<DS_GROUNDING>.persons` に人を登録する（所属ユニット = 見える範囲の実体, §3.3）
  2. メールアドレスで `oauth_identity_map` に事前登録する（本人の初回ログイン時に自動で紐づく）

組織シートで人員を管理している場合は、代わりに `sync_persons_from_sheet.py` と
`preregister_from_sheet.py` を使う。中で使う書き込み処理は同じもの。

名簿ファイルの形は `members.example.json`（記入例は `sample/members.json`）:

    {"members": [
      {"display_name": "山田 太郎", "email": "taro@example.org",
       "units": ["secretariat"], "roles": ["steward", "owner"]}
    ]}

- `units`  … 所属ユニットID（1つ以上）。組織全体のユニット（org.toml の [units] org）は自動で足す
- `roles`  … 任意。steward / owner / board / export_admin。org.toml の [units] の値に読み替える
- `email`  … 任意。無い人は事前登録できない（本人の初回ログイン後に管理者が紐づける）
- `person_id` … 任意。省略時は表示名から作る（組織シート同期と同じ規則）

名簿に書いた人は、名簿の内容で**置き換える**（roles を消して流し直すと、その役割も外れる）。
名簿に書いていない人には触れない。

名簿ファイルは個人情報を含むので、リポジトリには入れないこと（`/members.json` は .gitignore 済み）。

Usage:
    python scripts/register_members.py members.json            # 確認だけ（既定）
    python scripts/register_members.py members.json --apply    # 書き込む
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

UNIT_ID_RE = re.compile(r"[a-z][a-z0-9_]*")
PERSON_ID_RE = re.compile(r"p_[a-z0-9_]+")
ROLE_KEYS = ("steward", "owner", "board", "export_admin")


class MembersFileError(ValueError):
    """名簿ファイルの中身が登録できない形（どこを直すかをメッセージに書く）。"""


def _role_units(role: str, cfg) -> list[str]:
    units = cfg.units
    if role == "export_admin":
        if not units.export_admin:
            raise MembersFileError(
                "roles に export_admin がありますが、org.toml の [units] export_admin が空です"
            )
        return sorted(units.export_admin)
    return [getattr(units, role)]


def plan(data: object, cfg=None) -> tuple[list[dict], list[dict]]:
    """名簿 → (persons の行, 事前登録の行)。純粋関数（オフラインで検査できる）。

    推測はしない。足りない・おかしい値があれば、どの人のどの欄かを添えて止める。
    """
    from preregister_from_sheet import build_rows
    from sync_persons_from_sheet import _person_id

    from waca_loop.org import PLACEHOLDER, get_org_config

    cfg = cfg or get_org_config()
    if not isinstance(data, dict) or not isinstance(data.get("members"), list):
        raise MembersFileError('名簿ファイルは {"members": [ ... ]} の形にしてください')
    members = data["members"]
    if not members:
        raise MembersFileError("members が空です")

    org_unit = cfg.units.org
    people: list[dict] = []
    for n, m in enumerate(members, start=1):
        where = f"{n}人目"
        if not isinstance(m, dict):
            raise MembersFileError(f"{where}: 1人分は {{ ... }} の形で書いてください")
        if PLACEHOLDER in json.dumps(m, ensure_ascii=False).upper():
            raise MembersFileError(f"{where}: {PLACEHOLDER} が残っています")
        name = m.get("display_name")
        if not isinstance(name, str) or not name.strip():
            raise MembersFileError(f"{where}: display_name（表示名）がありません")
        name = name.strip()
        where = f"{n}人目（{name}）"

        units = m.get("units")
        if not isinstance(units, list) or not all(isinstance(u, str) for u in units):
            raise MembersFileError(
                f'{where}: units は ["secretariat"] のような配列で書いてください'
            )
        units = [u.strip() for u in units if u.strip()]
        bad = [u for u in units if not UNIT_ID_RE.fullmatch(u)]
        if bad:
            raise MembersFileError(
                f"{where}: ユニットIDは英小文字・数字・_ で書いてください: {bad}"
            )
        if not [u for u in units if u != org_unit]:
            # 組織全体のユニットだけの人は、何も所有できない（改善要望などの起票先が決まらない）。
            raise MembersFileError(
                f"{where}: 部署などのユニットを1つ以上書いてください"
                f"（部署が無い組織なら、例えば \"staff\" のような共通のユニットを作る）"
            )

        roles = m.get("roles", [])
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            raise MembersFileError(f"{where}: roles は配列で書いてください")
        unknown = [r for r in roles if r not in ROLE_KEYS]
        if unknown:
            raise MembersFileError(
                f"{where}: 知らない roles {unknown}。使えるのは {', '.join(ROLE_KEYS)}"
            )
        for r in roles:
            units.extend(_role_units(r, cfg))
        units.append(org_unit)
        seen: set[str] = set()
        unit_path = [u for u in units if not (u in seen or seen.add(u))]

        person_id = m.get("person_id") or _person_id(name)
        if not isinstance(person_id, str) or not PERSON_ID_RE.fullmatch(person_id):
            raise MembersFileError(
                f"{where}: person_id は p_ で始まる英小文字・数字・_ にしてください"
            )

        email = m.get("email") or ""
        if not isinstance(email, str):
            raise MembersFileError(f"{where}: email は文字列で書いてください")

        people.append({
            "person_id": person_id,
            "display_name": name,
            "unit_path": unit_path,
            "roles": list(roles),
            "_email": email.strip(),
        })

    dup_ids = sorted({p["person_id"] for p in people
                      if [x["person_id"] for x in people].count(p["person_id"]) > 1})
    if dup_ids:
        raise MembersFileError(
            f"person_id が重複しています（同姓同名なら person_id を書き分けてください）: {dup_ids}"
        )
    try:
        identity_rows = build_rows(people)   # アドレスの形・重複の検査もここで行う
    except ValueError as exc:
        raise MembersFileError(str(exc)) from None

    person_rows = [
        {
            "person_id": p["person_id"],
            "display_name": p["display_name"],
            "unit_path": p["unit_path"],
            "consent_internal": True,
            "consent_external": False,
            "circles": [],
            "status": "active",
            "_roles": p["roles"],
            "_email": p["_email"],
        }
        for p in people
    ]
    return person_rows, identity_rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("members_file", help="名簿ファイル（JSON）")
    parser.add_argument("--apply", action="store_true",
                        help="実際に BigQuery へ書き込む（既定は内容を表示するだけ）")
    args = parser.parse_args()

    from waca_loop.org import exit_if_incomplete, get_org_config

    if args.apply:
        # 実データに書く前に、組織設定（org.toml）が埋まっているかを確かめる。
        exit_if_incomplete()

    try:
        data = json.loads(Path(args.members_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: 名簿ファイルを読めません: {exc}")
        return 2
    try:
        person_rows, identity_rows = plan(data)
    except MembersFileError as exc:
        print(f"ERROR: {exc}")
        return 2

    cfg = get_org_config()
    print(f"組織: {cfg.name}　登録する人: {len(person_rows)}名")
    for p in person_rows:
        mail = p["_email"] or "（メール無し・事前登録しない）"
        print(f"   {p['person_id']:20s} {p['display_name']:12s} {p['unit_path']}  {mail}")
    granted = [(p["display_name"], p["_roles"]) for p in person_rows if p["_roles"]]
    if granted:
        print("\n役割の付与（権限そのものなので、内容を確かめてから --apply すること）:")
        for name, roles in granted:
            print(f"   {name}: {', '.join(roles)}")

    if not args.apply:
        print("\n確認だけ行いました。書き込むには --apply を付けて再実行してください。")
        return 0

    from datetime import UTC, datetime

    from preregister_from_sheet import apply_rows
    from sync_persons_from_sheet import upsert_persons

    from waca_loop.bq import make_client
    from waca_loop.config import get_settings

    settings = get_settings()
    if not settings.gcp_project:
        print("ERROR: GCP_PROJECT が未設定です（.env）", file=sys.stderr)
        return 2
    client = make_client(settings)
    now = datetime.now(UTC).isoformat()
    rows = [
        {k: v for k, v in p.items() if not k.startswith("_")} | {"created_at": now,
                                                                  "updated_at": now}
        for p in person_rows
    ]
    # persons が先。persons に行が無い person_id を事前登録すると、本人はログインできても
    # 全ツールが 403 になる（preregister_from_sheet.py と同じ順序）。
    upsert_persons(client, settings, rows)
    print(f"\npersons に {len(rows)}名を登録しました（{settings.persons_table}）")
    if identity_rows:
        apply_rows(client, settings.identity_map_table, identity_rows)
        print(f"事前登録 {len(identity_rows)}件（{settings.identity_map_table}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
