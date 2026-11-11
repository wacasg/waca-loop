"""Offline test for the org-sheet → persons parser (scripts/sync_persons_from_sheet.py).

Verifies cascade of 部門名/部名, unit_path derivation (committee → 部 → 部門 → org), email capture,
person_id override for OAuth-mapped people, and that it stays PII-minimal (no raw PII fields).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "sync_org_sheet",
    Path(__file__).resolve().parent.parent / "scripts" / "sync_persons_from_sheet.py",
)
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

HEADER = [
    "部門名", "担当理事", "理事補助", "部名", "部長", "副部長", "部付",
    "委員会名", "委員長", "メールアドレス", "副委員長", "メールアドレス", "事務局",
]


def _by_name(people):
    return {p["display_name"]: p for p in people}


def test_cascade_units_and_email():
    grid = [
        HEADER,
        ["部門A", "佐藤花子", "", "教材開発部", "鈴木四郎", "井上四郎", "",
         "委員会D", "木村三郎", "member1@example.com", "", "", ""],
        ["", "", "", "", "", "", "",
         "委員会A", "林五郎", "member2@example.net", "清水六郎", "member3@example.net", ""],
    ]
    people = _by_name(sync.parse_org_sheet(grid))

    # 委員長 gets committee → 部 → 部門 → org, plus its email.
    assert people["木村三郎"]["unit_path"] == [
        "c_d", "kyozai", "div_a", "org"
    ]
    assert people["木村三郎"]["_email"] == "member1@example.com"

    # 部名 cascades down to the next committee row (blank = inherit).
    assert people["林五郎"]["unit_path"] == [
        "c_a", "kyozai", "div_a", "org"
    ]
    # each role captures the メールアドレス column immediately to its right (incl. 副委員長).
    assert people["清水六郎"]["_email"] == "member3@example.net"

    # 担当理事 attaches at 部門 level; 部長 at 部 level.
    assert people["佐藤花子"]["unit_path"] == ["div_a", "org"]
    assert people["鈴木四郎"]["unit_path"] == ["kyozai", "div_a", "org"]


def test_person_id_override_preserves_oauth_mapping():
    grid = [
        HEADER,
        ["部門C", "伊藤五郎", "", "", "", "", "",
         "委員会　C", "山田一郎", "ichiro.yamada@example.org", "", "", ""],
    ]
    people = _by_name(sync.parse_org_sheet(grid))
    assert people["山田一郎"]["person_id"] == "p_yamada"  # override, not a hash


def test_person_directory_is_pii_minimal():
    grid = [HEADER, ["部門A", "佐藤花子", "", "", "", "", "", "", "", "", "", "", ""]]
    p = sync.parse_org_sheet(grid)[0]
    # only the minimal, non-PII directory fields (email is a transient hint, not a persons column)
    assert set(p) <= {
        "person_id", "display_name", "unit_path",
        "consent_internal", "consent_external", "status", "_email",
    }
    assert "birthdate" not in p and "phone" not in p


def test_manual_unit_grants_survive_a_resync():
    """シートから導けないユニット(steward 等)は再同期で消えてはならない.

    この同期は DELETE + APPEND なので、素朴に書き戻すと **スチュワードが0人になり
    昇格も承認も誰にもできなくなる**。さらに、行き場のないインシデント申告の
    寄せ先(steward)も消える。
    """
    derivable = set(sync.UNIT_MAP.values()) | {"org"}
    manual_units = sync.manual_units
    assert manual_units(["secretariat", "steward", "org"], derivable) == [
        "steward"
    ]
    assert manual_units(["org"], derivable) == []
    assert manual_units([], derivable) == []
    # シート由来のユニットは「手動付与」として二重に持ち回らない
    assert all(u not in manual_units(sorted(derivable), derivable) for u in derivable)


# --- 単位マスタタブ形式 ---

UNIT_MASTER = [
    ["部署コード", "単位名", "区分", "部門・所管", "部長・委員長", "メールアドレス",
     "担当理事", "備考"],
    ["D01", "事務局", "事務局", "理事会直轄", "伊藤五郎", "", "未定", ""],
    ["D02", "技術部", "部", "理事会直轄", "渡辺六郎", "", "—", ""],
    ["D03", "教材開発部", "部", "部門A", "鈴木四郎", "", "鈴木四郎（副：佐藤花子）", ""],
    ["D04", "委員会B", "委員会", "部門A", "中村七郎", "", "鈴木四郎", ""],
    ["D05", "会員部（第2期）", "部", "部門B", "小林八郎・加藤九郎", "",
     "吉田十郎（副：高橋二郎）", ""],
    ["D06", "広報部", "部", "理事会直下（所管）", "山本三郎（代行）", "",
     "高橋二郎", ""],
    [],
    ["部署コード", "単位名", "区分", "部門・所管", "氏名", "メールアドレス"],
    ["D01", "事務局", "事務局", "理事会直轄", "松本七郎", "shichiro.matsumoto@example.org"],
]


def _by_name(people):
    return {p["display_name"]: p for p in people}


def test_unit_master_reads_both_blocks_and_maps_every_unit():
    """単位マスタタブは「部長・委員長」ブロックと「氏名」ブロックの2段組で、どちらも人になる."""
    people, unmapped = sync.parse_unit_master_sheet(UNIT_MASTER)
    assert unmapped == []                       # 語彙の穴が無いこと
    got = _by_name(people)

    assert got["伊藤五郎"]["unit_path"] == ["secretariat", "org"]
    assert got["松本七郎"]["unit_path"] == ["secretariat", "org"]   # ②ブロック
    assert got["松本七郎"]["_email"] == "shichiro.matsumoto@example.org"
    # 単位名 → 部門・所管 → org の順に積む
    assert got["中村七郎"]["unit_path"] == ["c_b", "div_a", "org"]


def test_unit_master_handles_the_masters_notation():
    """括弧・中黒・プレースホルダの扱い — ここを誤ると人が消えるか幽霊が増える."""
    got = _by_name(sync.parse_unit_master_sheet(UNIT_MASTER)[0])

    assert "小林八郎" in got and "加藤九郎" in got        # 中黒で2名
    assert "会員部（第2期）" not in str(got)            # 括弧付きでも会員部として解決
    assert got["小林八郎"]["unit_path"][0] == "members"
    assert "山本三郎" in got and "代行" not in got    # 注記は氏名ではない
    assert "佐藤花子" in got                              # 「（副：X）」は実在の人なので拾う
    assert "—" not in got and "未定" not in got           # プレースホルダを人にしない
    # 担当理事も所属として数える（高橋さんは部長欄に一度も出てこない）
    assert "pr" in got["高橋二郎"]["unit_path"]


def test_unit_master_refuses_to_guess_an_unknown_unit():
    """語彙に無い単位名は unmapped として返す — org だけの人を黙って作らない.

    unmapped を無視して同期すると、その人は所有ユニット無し＝誰のものでもなく
    全員から見える状態で着地する（§3.3）。呼び出し側はこれを見て中止する。
    """
    grid = [
        ["部署コード", "単位名", "区分", "部門・所管", "部長・委員長"],
        ["XXX", "未来創造室", "部", "部門A", "誰か"],
    ]
    people, unmapped = sync.parse_unit_master_sheet(grid)
    assert unmapped == ["未来創造室"]
    assert _by_name(people)["誰か"]["unit_path"] == ["div_a", "org"]


def test_unit_master_is_case_insensitive_for_unit_slugs():
    """UNIT_MAP は小文字キー — 英字を含む「委員会B」が取りこぼされないこと."""
    assert sync._norm("委員会B") in sync.UNIT_MAP   # noqa: SLF001
    assert sync._norm("委員会C") in sync.UNIT_MAP   # noqa: SLF001


def test_grid_file_is_read_the_same_as_the_api(tmp_path):
    """手元書き出し(TSV/CSV/JSON)から読んでも API 経由と同じ結果になること.

    ADC に spreadsheets スコープを載せようとすると Google 側でブロックされる。
    追加の権限が要らない経路を残しておく。
    """
    import json

    header = "部署コード\t単位名\t区分\t部門・所管\t氏名\tメールアドレス"
    row = "D01\t事務局\t事務局\t理事会直轄\t松本七郎\tshichiro.matsumoto@example.org"
    expected = [header.split("\t"), row.split("\t")]

    tsv = tmp_path / "x.tsv"
    tsv.write_text(f"{header}\n{row}\n", encoding="utf-8")
    assert sync.read_grid_file(str(tsv)) == expected

    js = tmp_path / "x.json"
    js.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
    assert sync.read_grid_file(str(js)) == expected

    # Excel/Sheets の書き出しは BOM 付きのことがある — 先頭列名が壊れないこと
    bom = tmp_path / "b.csv"
    bom.write_text("﻿" + f"{header}\n{row}\n".replace("\t", ","), encoding="utf-8")
    assert sync.read_grid_file(str(bom))[0][0] == "部署コード"

    people, unmapped = sync.parse_unit_master_sheet(sync.read_grid_file(str(tsv)))
    assert unmapped == []
    assert people[0]["unit_path"] == ["secretariat", "org"]


def test_one_name_with_two_addresses_is_refused():
    """1つの氏名に別々のメールが付いていたら同期を止める.

    行はメールではなく**氏名でマージ**するので、同僚の行をコピーしてメールだけ書き換えると、
    2つ目のアドレスが黙って捨てられ、その人が**別人のアドレスで事前登録される**。
    台帳の帰属が入れ替わるので、推測せず止める。
    """
    grid = [
        ["部署コード", "単位名", "区分", "部門・所管", "氏名", "メールアドレス"],
        ["D01", "事務局", "事務局", "理事会直轄", "松本七郎", "goro.ito@example.org"],
        ["D01", "事務局", "事務局", "理事会直轄", "松本七郎", "shichiro.matsumoto@example.org"],
    ]
    people, unmapped = sync.parse_unit_master_sheet(grid)
    assert len(people) == 1                       # 氏名でマージされている
    assert unmapped and "松本七郎" in unmapped[0]  # だから止める
    assert "goro.ito@example.org" in unmapped[0]

    # 氏名が正しく分かれていれば通る
    grid[1][4] = "伊藤五郎"
    people, unmapped = sync.parse_unit_master_sheet(grid)
    assert unmapped == []
    assert {p["display_name"] for p in people} == {"伊藤五郎", "松本七郎"}
