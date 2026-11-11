"""Lite Sheets policy/adoption backend."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from waca_loop.models import Viewer
from waca_loop.policy import LiteSheetsPolicyStore, PolicySource


def test_lite_sheets_store_normalizes_guides_floors_channels_and_adoption_rows() -> None:
    store = LiteSheetsPolicyStore(
        {
            "policy_sources": [
                {
                    "source_id": "master",
                    "source_type": "policy_tab",
                    "title": "Lite master",
                    "tab_name": "kyozai",
                    "next_refresh_at": "2026-08-28T00:00:00+00:00",
                    "status": "active",
                }
            ],
            "policy_guides": [
                {
                    "guide_id": "g1",
                    "source_id": "doc1",
                    "source_type": "internal_policy",
                    "title": "AI活用方針",
                    "summary": "安全チャネルに寄せる",
                    "status": "trial",
                }
            ],
            "policy_floors": [
                {
                    "row_id": "FLOOR-01",
                    "line": "生PIIを安全でないツールへ入れない",
                    "effect": "仮名化してWACA Loopへ",
                    "trigger_kinds": "pii,credential",
                }
            ],
            "safe_channels": [
                {
                    "channel_id": "waca_loop",
                    "scope_unit": "kyozai",
                    "status": "approved",
                    "allowed_data_kinds": "pseudonymous",
                    "required_controls": "仮名化,最終判断は人",
                }
            ],
            "kyozai": [
                {
                    "No.": "1",
                    "業務": "教材レビュー",
                    "現状レベル(0-2)": "1",
                    "目標レベル(0-2)": "2",
                    "現状の使い方・安全な進め方": "公開情報だけで下書き",
                    "目標（次の一歩・期限）": "観点を下書きする",
                    "LL共有範囲": "unit",
                }
            ],
        },
        now=datetime(2026, 8, 28, tzinfo=UTC),
    )
    viewer = _viewer("p1", units={"kyozai"})

    assert [g.guide_id for g in store.guides_for(viewer)] == ["g1"]
    assert [f.row_id for f in store.floors_for(viewer)] == ["FLOOR-01"]
    assert store.safe_channels_for(viewer)[0].required_controls == ["仮名化", "最終判断は人"]
    assert store.rows_for_person(viewer)[0].business == "教材レビュー"
    assert store.refresh_due_sources(datetime(2026, 8, 28, 0, 0, 1, tzinfo=UTC)) == ["master"]


def test_lite_sheets_store_person_rows_are_private_unless_viewer_or_manager() -> None:
    store = LiteSheetsPolicyStore(
        {
            "policy_sources": [
                {"source_id": "edu", "source_type": "policy_tab", "tab_name": "kyozai"}
            ],
            "kyozai": [
                {
                    "No.": "1",
                    "actor_scope": "p1",
                    "業務": "本人が試したい分析",
                    "現状レベル(0-2)": "0",
                    "目標レベル(0-2)": "1",
                    "本人が使いたい業務": "自由記述の要約",
                    "試したいこと": "仮名化して傾向を見る",
                    "LL共有範囲": "person",
                },
                {
                    "No.": "2",
                    "actor_scope": "p1",
                    "業務": "上長と見る改善",
                    "現状レベル(0-2)": "1",
                    "上長指導内容": "安全チャネルで小さく試す",
                    "LL共有範囲": "manager",
                },
            ],
        }
    )

    p1 = _viewer("p1", units={"kyozai"})
    p2 = _viewer("p2", units={"kyozai"})
    steward = _viewer("m1", units={"steward"})

    assert [r.business for r in store.rows_for_person(p1)] == [
        "本人が試したい分析",
        "上長と見る改善",
    ]
    assert store.rows_for_person(p2) == []
    assert [r.business for r in store.rows_for_unit(steward, "kyozai")] == [
        "上長と見る改善"
    ]


def test_lite_sheets_refresh_failure_keeps_last_valid_snapshot() -> None:
    class BrokenReader:
        def read_source(self, source: PolicySource):
            raise RuntimeError("Sheets API down")

    source = PolicySource(
        source_id="master",
        source_type="sheet",
        title="Lite master",
        next_refresh_at=datetime(2026, 8, 28, tzinfo=UTC),
    )
    store = LiteSheetsPolicyStore(
        {
            "policy_sources": [
                {"source_id": "edu", "source_type": "policy_tab", "tab_name": "kyozai"}
            ],
            "kyozai": [
                {"No.": "1", "業務": "教材レビュー", "現状レベル(0-2)": "1"}
            ]
        },
        sources=[source],
        reader=BrokenReader(),
    )
    viewer = _viewer("p1", units={"kyozai"})

    result = store.refresh_source("master")

    assert result.success is False
    assert "last valid snapshot" in result.message
    assert [r.business for r in store.rows_for_person(viewer)] == ["教材レビュー"]


def test_refresh_error_message_has_no_exception_detail() -> None:
    class BrokenReader:
        def read_source(self, source: PolicySource):
            raise PermissionError("403 https://sheets.googleapis.com/token=secret")

    source = PolicySource(source_id="master", source_type="sheet", title="Lite master")
    store = LiteSheetsPolicyStore(
        {"policy_guides": [{"guide_id": "g", "summary": "要約"}]},
        sources=[source],
        reader=BrokenReader(),
    )

    result = store.refresh_source("master")

    assert result.success is False
    assert "auth" in result.message
    assert "sheets.googleapis.com" not in result.message
    assert "secret" not in result.message


def test_lite_sheets_refresh_marks_unchanged_snapshot() -> None:
    class SameReader:
        def read_source(self, source: PolicySource):
            return {
                "policy_sources": [
                    {"source_id": "edu", "source_type": "policy_tab", "tab_name": "kyozai"}
                ],
                "kyozai": [
                    {"No.": "1", "業務": "教材レビュー", "現状レベル(0-2)": "1"}
                ]
            }

    source = PolicySource(source_id="master", source_type="sheet", title="Lite master")
    store = LiteSheetsPolicyStore(
        {
            "policy_sources": [
                {"source_id": "edu", "source_type": "policy_tab", "tab_name": "kyozai"}
            ],
            "kyozai": [
                {"No.": "1", "業務": "教材レビュー", "現状レベル(0-2)": "1"}
            ]
        },
        sources=[source],
        reader=SameReader(),
    )

    result = store.refresh_source("master")

    assert result.success is True
    assert result.changed is False


def test_refresh_frequency_derives_and_advances_next_refresh_at() -> None:
    class SameReader:
        def read_source(self, source: PolicySource):
            return {"policy_guides": [{"guide_id": "g", "summary": "要約"}]}

    source = PolicySource(
        source_id="master",
        source_type="sheet",
        title="Lite master",
        refresh_frequency="weekly",
        last_refreshed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    store = LiteSheetsPolicyStore(
        {"policy_guides": [{"guide_id": "g", "summary": "要約"}]},
        sources=[source],
        reader=SameReader(),
    )

    assert store.refresh_due_sources(datetime(2026, 8, 28, tzinfo=UTC)) == ["master"]
    result = store.refresh_source("master")

    assert result.success is True
    assert "next_refresh_at=" in result.message
    assert store.refresh_due_sources(datetime(2026, 8, 28, tzinfo=UTC)) == []


def test_refresh_frequency_without_last_refresh_is_due_once() -> None:
    source = PolicySource(
        source_id="master",
        source_type="sheet",
        title="Lite master",
        refresh_frequency="weekly",
    )
    store = LiteSheetsPolicyStore(
        {"policy_guides": [{"guide_id": "g", "summary": "要約"}]},
        sources=[source],
    )

    assert store.refresh_due_sources(datetime(2026, 8, 28, tzinfo=UTC)) == ["master"]


def test_time_like_cells_are_normalized_to_aware_utc() -> None:
    store = LiteSheetsPolicyStore({
        "policy_guides": [
            {
                "guide_id": "future",
                "summary": "発効前",
                "status": "approved",
                "effective_at": "2999-01-01T00:00:00",
            }
        ],
        "policy_sources": [
            {
                "source_id": "master",
                "source_type": "sheet",
                "title": "Lite master",
                "next_refresh_at": "2026-08-28T00:00:00",
            }
        ],
    })

    assert store.guides_for(_viewer("p1", units={"kyozai"}))[0].status == "trial"
    assert store.refresh_due_sources(datetime(2026, 8, 29, tzinfo=UTC)) == ["master"]


def test_empty_snapshot_is_treated_as_failure() -> None:
    class EmptyReader:
        def read_source(self, source: PolicySource):
            return {}

    source = PolicySource(source_id="master", source_type="sheet", title="Lite master")
    store = LiteSheetsPolicyStore(
        {"policy_guides": [{"guide_id": "g", "summary": "要約"}]},
        sources=[source],
        reader=EmptyReader(),
    )

    result = store.refresh_source("master")

    assert result.success is False
    assert "empty_snapshot" in result.message
    assert [g.guide_id for g in store.guides_for(_viewer("p1"))] == ["g"]


def test_lite_sheets_store_drops_bad_level_row() -> None:
    store = LiteSheetsPolicyStore({
        "policy_sources": [
            {"source_id": "edu", "source_type": "policy_tab", "tab_name": "kyozai"}
        ],
        "kyozai": [
            {"No.": "1", "業務": "教材レビュー", "現状レベル(0-2)": "3"}
        ],
    })

    assert store.rows_for_person(_viewer("p1", units={"kyozai"})) == []


def test_lite_sheets_store_loads_json_snapshot_for_runtime_wiring(tmp_path) -> None:
    path = tmp_path / "lite_policy.json"
    path.write_text(
        json.dumps({
            "tabs": {
                "policy_sources": [
                    {
                        "source_id": "edu",
                        "source_type": "policy_tab",
                        "tab_name": "kyozai",
                    }
                ],
                "kyozai": [
                    {
                        "No.": "1",
                        "actor_scope": "p1",
                        "業務": "教材レビュー",
                        "現状レベル(0-2)": "1",
                        "目標レベル(0-2)": "2",
                        "試したいこと": "観点整理を下書きする",
                    }
                ]
            }
        }),
        encoding="utf-8",
    )

    store = LiteSheetsPolicyStore.from_json_file(path)

    out = store.rows_for_person(_viewer("p1", units={"kyozai"}))
    assert out[0].person_intent["try_next"] == "観点整理を下書きする"


def test_share_scope_japanese_values_map_to_person_and_unknown_fails_closed() -> None:
    store = LiteSheetsPolicyStore({
        "policy_sources": [
            {"source_id": "edu", "source_type": "policy_tab", "tab_name": "kyozai"}
        ],
        "kyozai": [
            {"No.": "1", "業務": "本人のみ", "現状レベル(0-2)": "1", "LL共有範囲": "本人のみ"},
            {"No.": "2", "業務": "謎", "現状レベル(0-2)": "1", "LL共有範囲": "謎"},
        ],
    })

    assert store.rows_for_unit(_viewer("p2", units={"kyozai"}), "kyozai") == []


def test_blank_share_scope_with_actor_fails_closed_to_person() -> None:
    store = LiteSheetsPolicyStore({
        "policy_sources": [
            {"source_id": "edu", "source_type": "policy_tab", "tab_name": "kyozai"}
        ],
        "kyozai": [
            {
                "No.": "1",
                "actor_scope": "p1",
                "業務": "共有範囲未記入の個人行",
                "現状レベル(0-2)": "1",
            }
        ],
    })

    assert store.rows_for_unit(_viewer("p2", units={"kyozai"}), "kyozai") == []
    rows = store.rows_for_person(_viewer("p1", units={"kyozai"}))
    assert [r.business for r in rows] == ["共有範囲未記入の個人行"]
    assert rows[0].share_scope == "person"


def test_manager_scope_requires_steward_for_unit_read() -> None:
    store = LiteSheetsPolicyStore({
        "policy_sources": [
            {"source_id": "edu", "source_type": "policy_tab", "tab_name": "kyozai"}
        ],
        "kyozai": [
            {
                "No.": "1",
                "actor_scope": "p1",
                "業務": "上長だけ",
                "現状レベル(0-2)": "1",
                "LL共有範囲": "上長",
            }
        ],
    })

    assert store.rows_for_unit(_viewer("p2", units={"kyozai"}), "kyozai") == []
    assert [r.business for r in store.rows_for_unit(
        _viewer("s", units={"steward"}), "kyozai"
    )] == ["上長だけ"]


def test_tab_not_in_policy_sources_is_ignored() -> None:
    store = LiteSheetsPolicyStore({
        "policy_sources": [
            {"source_id": "edu", "source_type": "policy_tab", "tab_name": "kyozai"}
        ],
        "メモ": [{"No.": "1", "業務": "古いメモ", "現状レベル(0-2)": "2"}],
    })

    assert store.rows_for_person(_viewer("p1", units={"メモ"})) == []


def test_actor_scope_accepts_workspace_email_and_rejects_external_email() -> None:
    store = LiteSheetsPolicyStore({
        "policy_sources": [
            {"source_id": "edu", "source_type": "policy_tab", "tab_name": "kyozai"}
        ],
        "kyozai": [
            {
                "No.": "1",
                "actor_scope": "tanaka@example.net",
                "業務": "社内メール",
                "現状レベル(0-2)": "1",
                "LL共有範囲": "本人のみ",
            },
            {
                "No.": "2",
                "actor_scope": "foo@mail.example.com",
                "業務": "外部メール",
                "現状レベル(0-2)": "1",
                "LL共有範囲": "本人のみ",
            },
        ],
    })

    rows = store.rows_for_person(_viewer("tanaka@example.net", units={"kyozai"}))
    assert [r.business for r in rows] == ["社内メール"]
    assert "tanaka@example.net" not in rows[0].row_id


def test_actor_scope_rejects_raw_google_sub() -> None:
    store = LiteSheetsPolicyStore({
        "policy_sources": [
            {"source_id": "edu", "source_type": "policy_tab", "tab_name": "kyozai"}
        ],
        "kyozai": [
            {
                "No.": "1",
                "actor_scope": "104538123456789012345",
                "業務": "raw sub",
                "現状レベル(0-2)": "1",
                "LL共有範囲": "本人のみ",
            }
        ],
    })

    assert store.rows_for_unit(_viewer("s", units={"steward"}), "kyozai") == []


def test_source_ref_has_no_url_or_token_and_policy_source_url_is_not_returned() -> None:
    store = LiteSheetsPolicyStore({
        "policy_sources": [
            {
                "source_id": "edu",
                "source_type": "policy_tab",
                "tab_name": "kyozai",
                "url": "https://docs.example.invalid/?token=secret",
            }
        ],
        "policy_guides": [
            {
                "guide_id": "g",
                "summary": "要約",
                "source_ref": "https://docs.example.invalid/?token=secret@example.invalid",
            }
        ],
        "kyozai": [
            {"No.": "1", "業務": "教材", "現状レベル(0-2)": "1"}
        ],
    })

    viewer = _viewer("p1", units={"kyozai"})
    text = str(store.guides_for(viewer) + store.rows_for_person(viewer))
    assert "https://docs.example.invalid" not in text
    assert "secret" not in text


def test_non_org_member_gets_nothing_from_all_lite_sheets_read_methods() -> None:
    store = LiteSheetsPolicyStore({
        "policy_sources": [
            {"source_id": "edu", "source_type": "policy_tab", "tab_name": "kyozai"}
        ],
        "policy_guides": [{"guide_id": "g", "summary": "要約"}],
        "policy_floors": [{"row_id": "FLOOR-01", "line": "床", "effect": "迂回"}],
        "safe_channels": [
            {"channel_id": "waca_loop", "scope_unit": "kyozai", "status": "approved"},
        ],
        "kyozai": [{"No.": "1", "業務": "教材", "現状レベル(0-2)": "1"}],
    })
    outsider = _viewer("x", units={"kyozai"}, is_org_member=False)

    assert store.guides_for(outsider) == []
    assert store.floors_for(outsider) == []
    assert store.safe_channels_for(outsider) == []
    assert store.rows_for_person(outsider) == []
    assert store.rows_for_unit(outsider, "kyozai") == []


def _viewer(
    person_id: str,
    *,
    units: set[str] | None = None,
    is_org_member: bool = True,
) -> Viewer:
    return Viewer(
        subject=f"sub:{person_id}",
        person_id=person_id,
        is_org_member=is_org_member,
        units=frozenset(units or set()),
    )
