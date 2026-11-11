"""事前登録によるオンボーディング — メールで先に登録し、初回ログインで sub に束ねる (§5)."""

from __future__ import annotations

from waca_loop.auth.oauth import Identity
from waca_loop.grounding.persons import (
    InMemoryPersonStore,
    normalize_email,
    pending_sub,
)
from waca_loop.models import Person

STAFF = Person(person_id="p_staff", display_name="事務局の職員",
               unit_path=["secretariat", "org"])


def _store() -> InMemoryPersonStore:
    return InMemoryPersonStore(
        persons=[STAFF],
        identity_map={pending_sub("Shiro.Suzuki@example.org", "p_staff"): "p_staff"},
    )


def test_first_login_binds_the_pending_row():
    store = _store()
    assert store.resolve_person_id("108...real") is None          # まだ紐づいていない
    claimed = store.claim_pending_registration("shiro.suzuki@example.org", "108...real")
    assert claimed == "p_staff"
    assert store.resolve_person_id("108...real") == "p_staff"     # 以後は通常の解決で通る


def test_the_claim_is_one_time():
    """保留行は引き当てで消える — 同じアドレスで別人が後から名乗っても通らない."""
    store = _store()
    assert store.claim_pending_registration("shiro.suzuki@example.org", "108...real")
    assert store.claim_pending_registration("shiro.suzuki@example.org", "999...other") is None
    assert store.resolve_person_id("999...other") is None


def test_email_matching_is_case_insensitive():
    store = _store()
    assert store.claim_pending_registration("SHIRO.SUZUKI@example.org", "108") == "p_staff"


def test_fullwidth_domain_still_faces_the_hd_gate():
    """全角ドメインで hd 要求を迂回できないこと (§5).

    正規化しなければ「ｅｘａｍｐｌｅ.org」は WORKSPACE_DOMAINS と文字列一致せず、**組織ドメインの
    アドレスなのに hd クレーム必須の判定を素通り**する。個人アカウントで組織アドレスの
    保留行を引き当てられる経路なので、NFKC はログイン可否だけでなくここも塞いでいる。
    """
    store = InMemoryPersonStore(
        persons=[STAFF],
        identity_map={pending_sub("shiro.suzuki@example.org", "p_staff"): "p_staff"},
    )
    spoofed = Identity(subject="108", email="shiro.suzuki＠ｅｘａｍｐｌｅ.org",
                       email_verified=True, hd=None)   # hd 無し = 個人アカウント
    assert _provider(store)._claim_pending(spoofed) is False       # noqa: SLF001
    assert store.resolve_person_id("108") is None


def test_an_unregistered_address_claims_nothing():
    assert _store().claim_pending_registration("stranger@example.com", "108") is None


def test_unverified_email_must_not_claim():
    """検証されていないメールでは引き当てない — ここが崩れると許可リストの意味が無くなる."""
    store = _store()
    unverified = Identity(subject="108", email="shiro.suzuki@example.org",
                          email_verified=False, hd="example.org")
    assert _provider(store)._claim_pending(unverified) is False   # noqa: SLF001
    assert store.resolve_person_id("108") is None

    ok = Identity(subject="108", email="shiro.suzuki@example.org",
                  email_verified=True, hd="example.org")
    assert _provider(store)._claim_pending(ok) is True            # noqa: SLF001
    assert store.resolve_person_id("108") == "p_staff"


def test_pending_sub_can_never_collide_with_a_google_sub():
    """Google の sub は数字のみ — 番兵と衝突しないこと."""
    assert pending_sub("a@example.jp", "p_x").startswith("pending:")
    assert not pending_sub("a@example.jp", "p_x").isdigit()
    assert normalize_email("  A@EXAMPLE.JP ") == "a@example.jp"


def test_fullwidth_at_sign_normalizes_to_ascii():
    """全角＠で書かれたアドレスが Google の claim と突き合わせ可能になること (§5).

    IME で打つと「＠」(U+FF20) が混じる。見た目は正しいので人のレビューを通り抜け、
    事前登録は成功したように見えるのに本人は永久にログインできない。
    番兵キーも同じ関数を通るので、両側が同じ ASCII に落ちることまで見る。
    """
    assert normalize_email("goro.ito＠example.com") == "goro.ito@example.com"
    assert normalize_email("ＡＢＣ＠ｅｘａｍｐｌｅ．ｃｏｍ") == "abc@example.com"
    assert pending_sub("goro.ito＠example.com", "p_x") == pending_sub(
        "goro.ito@example.com", "p_x"
    )


def test_two_people_on_one_address_are_refused_not_guessed():
    """同じアドレスに2人ぶら下がっていたら引き当てない — 片方に他方の可視範囲を渡さない.

    person_id は氏名から作るので、シート上の改名や共用メールボックスでこれが起きる。
    """
    store = InMemoryPersonStore(
        persons=[STAFF],
        identity_map={
            pending_sub("shared@example.org", "p_a"): "p_a",
            pending_sub("shared@example.org", "p_b"): "p_b",
        },
    )
    assert store.claim_pending_registration("shared@example.org", "108") is None
    assert store.resolve_person_id("108") is None


def test_preregistration_rows_refuse_a_duplicated_address():
    """同上をシート読み込みの段階でも弾く（BQ へ持ち込ませない）."""
    import importlib.util
    from pathlib import Path

    import pytest

    spec = importlib.util.spec_from_file_location(
        "prereg", Path(__file__).resolve().parent.parent / "scripts" / "preregister_from_sheet.py"
    )
    pre = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pre)

    people = [
        {"person_id": "p_a", "display_name": "A", "_email": "shared@example.org"},
        {"person_id": "p_b", "display_name": "B", "_email": "SHARED@example.org"},
    ]
    with pytest.raises(ValueError, match="複数人"):
        pre.build_rows(people)

    with pytest.raises(ValueError, match="不正なメールアドレス"):
        pre.build_rows([{"person_id": "p_c", "display_name": "C", "_email": "not an address'"}])


# --- Workspace ドメインの hd 検査 ---
#
# example.org / example.net は同一組織の Workspace。email_verified は「今そのメールボックスを
# 持っている」ことしか示さないので、退職者アドレスの再割り当てで前任者の可視範囲が渡りうる。
# 組織ドメインのアドレスは、その Workspace が発行したアカウントでなければ引き当てさせない。


class _Settings:
    def __init__(self, domains=("example.org", "example.net")):
        self.workspace_domains_set = frozenset(domains)


def _provider(store, settings=None):
    from waca_loop.auth.as_provider import GoogleBrokeringAuthProvider

    obj = object.__new__(GoogleBrokeringAuthProvider)
    obj._persons = store                      # noqa: SLF001
    obj._settings = settings or _Settings()   # noqa: SLF001
    return obj


def _staff_store(email="shiro.suzuki@example.org"):
    return InMemoryPersonStore(
        persons=[STAFF], identity_map={pending_sub(email, "p_staff"): "p_staff"}
    )


def test_workspace_address_needs_a_matching_hd_claim():
    store = _staff_store()
    ident = Identity(subject="108", email="shiro.suzuki@example.org",
                     email_verified=True, hd=None)          # 個人アカウント
    assert _provider(store)._claim_pending(ident) is False   # noqa: SLF001
    assert store.resolve_person_id("108") is None

    wrong = Identity(subject="108", email="shiro.suzuki@example.org",
                     email_verified=True, hd="example.com")  # 別組織の Workspace
    assert _provider(store)._claim_pending(wrong) is False    # noqa: SLF001

    # 同一組織のもう一方のドメインで発行されたアカウントは通す（マルチドメインテナントでは
    # 副ドメインのアドレスに主ドメインの hd が付くことがある。等値を要求すると締め出す）
    sibling = Identity(subject="108", email="shiro.suzuki@example.org",
                       email_verified=True, hd="example.net")
    assert _provider(store)._claim_pending(sibling) is True   # noqa: SLF001

    store2 = _staff_store()
    ok = Identity(subject="109", email="shiro.suzuki@example.org",
                  email_verified=True, hd="example.org")
    assert _provider(store2)._claim_pending(ok) is True       # noqa: SLF001
    assert store2.resolve_person_id("109") == "p_staff"


def test_the_second_org_domain_works_the_same_way():
    """example.net も同一組織 — 同じ規則が効くこと."""
    store = _staff_store("ichiro@example.net")
    ident = Identity(subject="777", email="ichiro@example.net",
                     email_verified=True, hd="example.net")
    assert _provider(store)._claim_pending(ident) is True     # noqa: SLF001


def test_outside_addresses_are_unaffected_by_the_hd_rule():
    """gmail.com 等は hd を持たない — 組織ドメイン以外に hd を要求しない."""
    store = _staff_store("someone@mail.example.com")
    ident = Identity(subject="555", email="someone@mail.example.com", email_verified=True, hd=None)
    assert _provider(store)._claim_pending(ident) is True     # noqa: SLF001


def test_the_claim_leaves_an_audit_trail():
    """§5: 引き当ては行を上書きするので、経路を記録しないと事後に区別できない.

    上書き後の行は「管理者が sub を直接入れた行」と字面が同じになる。自動の束ね付けと
    人間の判断による付与は、アクセス権の監査でいちばん見たい差である。
    """
    from waca_loop.grounding.persons import CLAIM_SOURCE_PREREGISTRATION

    store = _store()
    assert store._claims == {}  # noqa: SLF001 — 引き当て前は何も無い
    store.claim_pending_registration("shiro.suzuki@example.org", "108...real")

    claim = store._claims["108...real"]  # noqa: SLF001
    assert claim["claim_source"] == CLAIM_SOURCE_PREREGISTRATION
    assert claim["claimed_at"] is not None


def test_a_refused_claim_records_nothing():
    """束ねられなかった試行は痕跡を残さない(未登録アドレスで痕跡を作らせない)."""
    store = _store()
    assert store.claim_pending_registration("stranger@example.org", "999") is None
    assert store._claims == {}  # noqa: SLF001


def test_the_roster_can_only_move_an_address_between_domains():
    """事前登録の MERGE が、保留行の宛先を「別ドメインへの移動」にしか使えないこと (§5).

    シートは BigQuery 書き込み権限より多くの人が編集できる。ローカル部の固定が無いと、
    シート編集者がまだログインしていない人の行を自分の管理するアドレスへ向け、次の適用の
    あとで初回ログインし、**他人の person_id とユニット権限**に束ねられる — しかも監査痕跡は
    それを preregistration(＝正規) として記録する。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import preregister_from_sheet as pre

    src = Path(pre.__file__).read_text(encoding="utf-8")
    merge = src[src.index("MERGE `{table}`"):]
    update_clause = merge[merge.index("WHEN MATCHED"):merge.index("WHEN NOT MATCHED")]

    # 既にログイン済みの行は触らない
    assert "STARTS_WITH(t.oauth_sub, '{PENDING_SUB_PREFIX}')" in update_clause
    # ローカル部が変わる付け替えは UPDATE の対象にならない
    assert "SPLIT(t.email, '@')[OFFSET(0)] = SPLIT(s.email, '@')[OFFSET(0)]" in update_clause


def test_two_people_with_the_same_name_are_refused():
    """person_id は表示名から導出される — 同姓同名は同じ id になるので拒否する."""
    import sys
    from pathlib import Path

    import pytest

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import preregister_from_sheet as pre

    people = [
        {"person_id": "p_same", "display_name": "同じ名前", "_email": "a@example.net"},
        {"person_id": "p_same", "display_name": "同じ名前", "_email": "b@example.net"},
    ]
    with pytest.raises(ValueError, match="同姓同名"):
        pre.build_rows(people)


def test_either_org_domain_claims_the_same_person():
    """A: 一人が組織の二つのドメインを行き来しても、どちらでも通ること (§5).

    `ichiro@example.net` で登録された人が `@example.org` で弾かれてはならない。
    「保留行がちょうど1つ」で切るとそうなる。危険なのは行数ではなく**別人を指す行が
    混ざっていること**である。
    """
    store = InMemoryPersonStore(
        persons=[STAFF],
        identity_map={
            pending_sub("ichiro@example.org", "p_staff"): "p_staff",
            pending_sub("ichiro@example.net", "p_staff"): "p_staff",
        },
    )
    assert store.claim_pending_registration("ichiro@example.org", "108") == "p_staff"
    assert store.resolve_person_id("108") == "p_staff"


def test_claiming_one_address_does_not_disarm_another_addresss_refusal():
    """★ 消費するのは「いま証明したアドレスの行」だけ.

    「その人の保留行を残らず消す」実装は、**別アドレスの拒否を解除する**。共有メールボックスの拒否は「2人を指す行が並んでいる」ことで成立して
    おり、拒否はその行でできている。片方を消すと拒否が消え、そのメールボックスを持つ
    誰かが残った1人に束ねられる — しかも痕跡は preregistration(正規)と記録される。
    """
    other = Person(person_id="p_other", display_name="別の人",
                   unit_path=["kyozai", "org"])
    store = InMemoryPersonStore(
        persons=[STAFF, other],
        identity_map={
            pending_sub("ichiro@example.net", "p_staff"): "p_staff",
            "pending:p_staff:info@example.org": "p_staff",   # 共有メールボックス
            "pending:p_other:info@example.org": "p_other",   # 同じ宛先に別人
        },
    )
    store.claim_pending_registration("ichiro@example.net", "108")

    # 共有メールボックスは依然として「2人を指す」ので拒否され続けること
    assert store.claim_pending_registration("info@example.org", "999") is None


def test_a_frozen_person_cannot_be_claimed():
    """退職者の保留行を、アドレス再割当の受け皿にしない (§5).

    旧担当者のアドレスが新任者へ回ると、保留行がそのまま引き当て先になり、
    **他人のユニット権限をそのまま引き継ぐ**。運用(シートから消す)だけに頼らない。
    """
    left = Person(person_id="p_left", display_name="退職者",
                  unit_path=["secretariat", "org"], status="frozen")
    store = InMemoryPersonStore(
        persons=[left],
        identity_map={pending_sub("shiro@example.org", "p_left"): "p_left"},
    )
    assert store.claim_pending_registration("shiro@example.org", "108") is None


def test_two_different_people_on_one_address_are_still_refused():
    """緩めたのは「同じ人の複数アドレス」だけ。別人が混ざる場合は拒否."""
    other = Person(person_id="p_other", display_name="別の人",
                   unit_path=["kyozai", "org"])
    store = InMemoryPersonStore(
        persons=[STAFF, other],
        identity_map={
            "pending:p_staff:shared@example.org": "p_staff",
            "pending:p_other:shared@example.org": "p_other",
        },
    )
    assert store.claim_pending_registration("shared@example.org", "108") is None
