"""名簿ファイルからの登録（scripts/register_members.py）— 推測せず、直す場所を名指しして止まる."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import register_members as reg  # noqa: E402


def _one(**overrides):
    member = {"display_name": "山田 太郎", "email": "taro@example.org", "units": ["secretariat"]}
    member.update(overrides)
    return {"members": [member]}


def test_the_sample_roster_is_registrable():
    data = json.loads((ROOT / "sample" / "members.json").read_text(encoding="utf-8"))
    persons, identity = reg.plan(data)
    assert len(persons) == 5
    assert len(identity) == 4                    # メールの無い1名は事前登録しない
    assert all(p["unit_path"][-1] == "org" for p in persons)


def test_the_template_refuses_until_filled_in():
    data = json.loads((ROOT / "members.example.json").read_text(encoding="utf-8"))
    with pytest.raises(reg.MembersFileError, match="CHANGE-ME"):
        reg.plan(data)


def test_roles_become_the_configured_units_and_org_is_appended():
    persons, _ = reg.plan(_one(roles=["steward", "owner", "export_admin"]))
    assert persons[0]["unit_path"] == ["secretariat", "steward", "owner", "export_admin", "org"]


def test_pending_rows_are_built_from_the_email():
    persons, identity = reg.plan(_one(email="Taro@Example.org"))
    assert identity[0]["email"] == "taro@example.org"
    assert identity[0]["person_id"] == persons[0]["person_id"]
    assert identity[0]["oauth_sub"].startswith("pending:")


def test_a_member_with_only_the_org_unit_is_refused():
    """組織全体のユニットだけの人は何も所有できない — 黙って登録しない."""
    with pytest.raises(reg.MembersFileError, match="ユニットを1つ以上"):
        reg.plan(_one(units=["org"]))
    with pytest.raises(reg.MembersFileError, match="ユニットを1つ以上"):
        reg.plan(_one(units=[]))


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"display_name": ""}, "display_name"),
        ({"units": "secretariat"}, "配列"),
        ({"units": ["Secretariat"]}, "英小文字"),
        ({"roles": ["admin"]}, "知らない roles"),
        ({"person_id": "yamada"}, "person_id"),
        ({"email": "not an address"}, "不正なメールアドレス"),
    ],
)
def test_bad_values_name_what_to_fix(override, message):
    with pytest.raises(reg.MembersFileError, match=message):
        reg.plan(_one(**override))


def test_duplicates_are_refused_not_guessed():
    two_ids = {"members": [
        {"display_name": "A", "units": ["x"], "person_id": "p_same"},
        {"display_name": "B", "units": ["y"], "person_id": "p_same"},
    ]}
    with pytest.raises(reg.MembersFileError, match="person_id が重複"):
        reg.plan(two_ids)
    two_mails = {"members": [
        {"display_name": "A", "units": ["x"], "email": "same@example.org"},
        {"display_name": "B", "units": ["y"], "email": "same@example.org"},
    ]}
    with pytest.raises(reg.MembersFileError, match="同じメールアドレス"):
        reg.plan(two_mails)


def test_export_admin_role_needs_the_unit_to_be_configured():
    from dataclasses import replace

    from waca_loop.org import get_org_config

    cfg = get_org_config()
    no_admin = replace(cfg, units=replace(cfg.units, export_admin=frozenset()))
    with pytest.raises(reg.MembersFileError, match="export_admin が空"):
        reg.plan(_one(roles=["export_admin"]), no_admin)


def test_the_file_shape_is_checked():
    with pytest.raises(reg.MembersFileError, match="members"):
        reg.plan([{"display_name": "A"}])
    with pytest.raises(reg.MembersFileError, match="空"):
        reg.plan({"members": []})


def test_the_sample_terms_and_policy_load():
    """sample/ の用語・ポリシーは、投入スクリプトがそのまま受け付ける形であること."""
    import seed_policy
    import seed_terms

    terms = json.loads((ROOT / "sample" / "terms.json").read_text(encoding="utf-8"))
    rows = seed_terms._rows(terms)                                   # noqa: SLF001
    assert len(rows) == 5 and all(r["status"] == "approved" for r in rows)
    rules = json.loads((ROOT / "sample" / "policy.json").read_text(encoding="utf-8"))
    prow = seed_policy._rows(rules)                                  # noqa: SLF001
    assert {r["row_id"] for r in prow if r["is_floor"]} == {"FLOOR-01", "FLOOR-02"}
