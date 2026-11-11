"""引き当ての SQL の形を固定する (§5) — LIKE を使わないこと + 保留行を残らず消費すること.

インメモリ双子は厳密な ``endswith`` で照合するため、BigQuery 側だけが ``LIKE`` を
使うという食い違いは、**双子を比べるオフラインのテストでは再現できない**。
`_` はメールのローカル部でありふれた文字でありながら LIKE では任意の1文字に化ける。
ここではストアが組み立てる SQL の形そのものを固定する。
"""

from __future__ import annotations

import pytest

from waca_loop.grounding.persons import BigQueryPersonStore


class _FakeJob:
    def __init__(self, rows, affected: int = 2):
        self._rows = rows
        # 2 にしてある: 同じ人に複数の保留行があるのが本番の形で、`!= 1` で弾く実装だと
        # **引き当ては済んでいるのにログインだけ拒否**される。1 を返す偽物を置くと、
        # その不具合がテストから構造的に見えなくなる。
        self.num_dml_affected_rows = affected

    def result(self):
        return iter(self._rows)


class _FakeClient:
    """Records every SQL it is handed; answers the COUNT with a single match."""

    def __init__(self):
        self.queries: list[str] = []
        self.params: list[list] = []

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        self.params.append(list(job_config.query_parameters) if job_config else [])
        return _FakeJob([{"person_id": "p_staff", "status": "active"}])


class _FakeSettings:
    identity_map_table = "proj.ds.oauth_identity_map"
    persons_table = "proj.ds.persons"


def test_the_claim_never_puts_the_address_into_a_like_pattern():
    pytest.importorskip("google.cloud.bigquery")

    client = _FakeClient()
    store = BigQueryPersonStore(client, _FakeSettings())
    store.claim_pending_registration("taro_yamada@example.org", "108...real")

    assert client.queries, "no SQL was issued"
    for sql in client.queries:
        assert "LIKE" not in sql.upper(), f"address matched with LIKE: {sql}"

    # 保留行を探す2本（別人混在の検査と引き当て）が、どちらも厳密照合であること
    matching = [q for q in client.queries if "@suffix" in q]
    assert len(matching) == 2
    for sql in matching:
        assert "STARTS_WITH(" in sql and "ENDS_WITH(" in sql

    # 照合の値はアドレスそのもの — パターン化されていないこと
    suffixes = {p.value for ps in client.params for p in ps if p.name == "suffix"}
    assert suffixes == {":taro_yamada@example.org"}


def test_both_the_count_guard_and_the_update_use_the_same_matcher():
    """COUNT の守りと UPDATE が別の条件だと、守りをすり抜けた行が書き換わる."""
    pytest.importorskip("google.cloud.bigquery")

    client = _FakeClient()
    BigQueryPersonStore(client, _FakeSettings()).claim_pending_registration(
        "a.b@example.org", "999"
    )
    guard_sql = next(q for q in client.queries if "SELECT DISTINCT" in q)
    update_i = next(i for i, q in enumerate(client.queries) if "UPDATE" in q)
    update_sql = client.queries[update_i]
    matcher = "STARTS_WITH(oauth_sub, @prefix) AND ENDS_WITH(oauth_sub, @suffix)"
    assert matcher.replace("oauth_sub", "m.oauth_sub") in guard_sql
    assert matcher in update_sql
    # 守りが1人に絞ったなら、書く側も**その人に限る**こと。@pid を bind しながら
    # WHERE で使っておらず、そのアドレスの保留行を人を問わず全部書き換えていた。
    assert "person_id = @pid" in update_sql
    assert any(p.name == "pid" for p in client.params[update_i])
    # 消費するのは証明したアドレスの行だけ — 他アドレスの行を消すと別の拒否が崩れる
    assert "DELETE" not in update_sql


def test_the_guard_counts_before_it_drops_retired_people():
    """守りの順序を双子に揃える — 数えてから退職者を弾く。

    JOIN の WHERE に `p.status = 'active'` を置くと、退職者の行が DISTINCT の前に消える。
    「現職A＋退職者B が同じ共有メールボックスを指す」状態が **1人に見えて通り**、InMemory
    双子（全行で数えてから active を見る）だけが拒否する、という食い違いになる。この関数は
    オフラインテストが届かないので、SQL の形で固定する。
    """
    pytest.importorskip("google.cloud.bigquery")

    client = _FakeClient()
    BigQueryPersonStore(client, _FakeSettings()).claim_pending_registration(
        "info@example.org", "777"
    )
    guard_sql = next(q for q in client.queries if "SELECT DISTINCT" in q)
    assert "LEFT JOIN" in guard_sql, "退職者の行が数える前に消える JOIN になっている"
    assert "p.status = 'active'" not in guard_sql, "status を WHERE で絞ってはいけない"
    assert "status" in guard_sql, "status を取り出していない（退職者を弾けない）"


def test_a_retired_row_on_the_same_address_blocks_the_claim():
    """現職A＋退職者B が同じアドレスを指すなら、引き当てを拒否して UPDATE を撃たない."""
    pytest.importorskip("google.cloud.bigquery")

    class _MixedClient(_FakeClient):
        def query(self, sql, job_config=None):
            self.queries.append(sql)
            self.params.append(list(job_config.query_parameters) if job_config else [])
            return _FakeJob([
                {"person_id": "p_active", "status": "active"},
                {"person_id": "p_retired", "status": "frozen"},
            ])

    client = _MixedClient()
    got = BigQueryPersonStore(client, _FakeSettings()).claim_pending_registration(
        "info@example.org", "777"
    )
    assert got is None
    assert not any("UPDATE" in q for q in client.queries), "拒否したのに UPDATE が撃たれている"


def test_one_person_two_addresses_still_claims():
    """同一人物が複数行（別ドメイン）を持つ通常形は通ること."""
    pytest.importorskip("google.cloud.bigquery")

    class _SamePerson(_FakeClient):
        def query(self, sql, job_config=None):
            self.queries.append(sql)
            self.params.append(list(job_config.query_parameters) if job_config else [])
            return _FakeJob([
                {"person_id": "p_staff", "status": "active"},
                {"person_id": "p_staff", "status": "active"},
            ])

    client = _SamePerson()
    got = BigQueryPersonStore(client, _FakeSettings()).claim_pending_registration(
        "taro@example.org", "111"
    )
    assert any("UPDATE" in q for q in client.queries)
    # 「UPDATE が撃たれた」だけでは、引き当て結果を返さない退行を見逃す。
    # 複数行が更新されたときに `!= 1` で弾く実装に戻ると、記録は済んでいるのに
    # ログインだけ拒否される。
    assert got == "p_staff", "引き当ては成立しているのに person_id を返していない"
