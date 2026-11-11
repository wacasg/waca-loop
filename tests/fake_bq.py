"""行を返す偽 BigQuery クライアント — 組み立てた SQL を SQLite で**実際に評価する**。

**なぜ要るか**（docs/TEST_STRATEGY.md §2.4 条件4b）。オフラインのスイートは BigQuery を
呼べないので、SQL を記録するだけの偽クライアントでは、双子の BigQuery 側は「SQL を組み立てる
ところまで」しか見られない。それだと *ゲート句が入っているか* は言えても *見えない行が
返らないか* は言えない。

ここでは BigQuery の SQL を SQLite の方言へ機械的に翻訳し、インメモリの表に対して
評価する。**翻訳できない構文は黙って無視せず落とす** — 未対応を「問題なし」として
緑にするのが、この手の道具の壊れ方だから。

対応範囲は「いま双子が実際に使っている構文」に限る（増えたら足す）。ここは本物の
BigQuery の代用ではなく、**双子が同じ行を返すことを表明するための土台**である。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from typing import Any

# 配列（REPEATED）は JSON 文字列として持つ。SQLite に配列型が無いため。
_ARRAY_COLUMNS = {"unit_path", "circles", "policy_row_ids", "trigger_kinds", "embedding",
                  "data_kinds", "source_refs", "proposed_circles"}


class UnsupportedSql(AssertionError):
    """翻訳できない SQL。**握り潰さない** — 未検査を緑にしないため。"""


def _table_key(ref: str) -> str:
    return ref.strip("`").replace(".", "__").replace("-", "_")


# MERGE は SQLite に無い。単一行を USING する形（`BigQueryTermStore.add` が使う upsert）に
# 限れば「UPDATE して 0 行なら INSERT」と等価なので、その2文に開いて実際に評価する。
# **この形以外は翻訳せず落とす**（UnsupportedSql）— 未対応を緑にしないため。
_MERGE_RE = re.compile(
    r"MERGE\s+(?P<table>`[^`]+`)\s+AS\s+T\s+"
    r"USING\s*\(\s*SELECT\s+@(?P<key_param>\w+)\s+AS\s+(?P<key_col>\w+)\s*\)\s*AS\s+S\s+"
    r"ON\s+T\.(?P=key_col)\s*=\s*S\.(?P=key_col)\s+"
    r"WHEN\s+MATCHED\s+THEN\s+UPDATE\s+SET\s+(?P<setlist>.+?)\s+"
    r"WHEN\s+NOT\s+MATCHED\s+THEN\s+INSERT\s*\((?P<cols>.+?)\)\s*"
    r"VALUES\s*\((?P<vals>.+)\)\s*$",
    re.S | re.I,
)


def merge_to_upsert(sql: str) -> tuple[str, str, str]:
    """MERGE を (UPDATE 文, INSERT 文, キー列) に開く。対応外の形なら :class:`UnsupportedSql`."""
    m = _MERGE_RE.search(sql.strip())
    if not m:
        raise UnsupportedSql(f"この形の MERGE は翻訳できない: {sql[:120]}...")
    table, key_col, key_param = m["table"], m["key_col"], m["key_param"]
    # `T.` / `S.` の別名は SQLite の単表 UPDATE では使えないので落とす。
    setlist = re.sub(r"\b[TS]\.", "", m["setlist"])
    update = f"UPDATE {table} SET {setlist} WHERE {key_col} = @{key_param}"
    insert = f"INSERT INTO {table} ({m['cols']}) VALUES ({m['vals']})"
    return update, insert, key_col


def _split_args(inner: str) -> list[str]:
    """``a, f(b, c), d`` → 3要素。括弧の中のカンマで切らない。"""
    args, depth, buf = [], 0, ""
    for ch in inner:
        if ch == "," and depth == 0:
            args.append(buf.strip())
            buf = ""
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        buf += ch
    if buf.strip():
        args.append(buf.strip())
    return args


def _translate(sql: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """BigQuery SQL → SQLite。対応外に当たったら :class:`UnsupportedSql`。"""
    out = sql

    # `proj.ds.table` → proj__ds__table
    out = re.sub(r"`([\w.\-]+)`", lambda m: _table_key(m.group(1)), out)

    # x IN UNNEST(@p) → x IN (:p_0, :p_1, ...)   ／ 空配列は常に偽
    def _unnest(m: re.Match) -> str:
        name = m.group(1)
        values = params.get(name) or []
        if not values:
            return "0"
        return "(" + ", ".join(f":{name}_{i}" for i in range(len(values))) + ")"

    out = re.sub(r"UNNEST\(@(\w+)\)", _unnest, out)

    # 関数の翻訳
    out = re.sub(r"STARTS_WITH\(([^,]+),\s*([^)]+)\)", r"(substr(\1, 1, length(\2)) = \2)", out)
    out = re.sub(r"ENDS_WITH\(([^,]+),\s*([^)]+)\)", r"(substr(\1, -length(\2)) = \2)", out)
    out = re.sub(r"STRPOS\(([^,]+),\s*([^)]+)\)", r"instr(\1, \2)", out)
    out = re.sub(r"ARRAY_LENGTH\(([^)]+)\)", r"json_array_length(\1)", out)
    out = out.replace("IFNULL(", "ifnull(").replace("COALESCE(", "coalesce(")
    out = out.replace("CURRENT_TIMESTAMP()", "datetime('now')")

    # PARSE_JSON(x) → x（SQLite に JSON 型は無い。attributes は文字列のまま保持する）
    out = re.sub(r"PARSE_JSON\(([^()]*)\)", r"\1", out)

    # IF(cond, a, b) → iif(cond, a, b)（SQLite 3.32+）。入れ子の括弧をそのまま通せるので、
    # 引数を自前で切らずに済む。
    out = re.sub(r"\bIF\(", "iif(", out)

    # @param → :param
    out = re.sub(r"@(\w+)", r":\1", out)

    # TIMESTAMP_SUB(<ts>, INTERVAL <n|:param> DAY) → datetime(<ts>, '-<n> days')
    # 比較相手の列は ISO 8601（'YYYY-MM-DDTHH:MM:SS…'）の文字列なので、結果も同じ形にそろえる。
    # datetime() の 'YYYY-MM-DD HH:MM:SS' のままだと、同じ日付の時刻を文字列比較で取り違える。
    out = re.sub(
        r"TIMESTAMP_SUB\(\s*([^,]+?)\s*,\s*INTERVAL\s+(:?\w+)\s+DAY\s*\)",
        lambda m: f"strftime('%Y-%m-%dT%H:%M:%S', {m.group(1)}, '-' || {m.group(2)} || ' days')"
        if m.group(2).startswith(":")
        else f"strftime('%Y-%m-%dT%H:%M:%S', {m.group(1)}, '-{m.group(2)} days')",
        out,
    )

    # 同じことを分単位でも（再送の重複排除は分オーダーの窓を使う）
    out = re.sub(
        r"TIMESTAMP_SUB\(\s*([^,]+?)\s*,\s*INTERVAL\s+(:?\w+)\s+MINUTE\s*\)",
        lambda m: f"strftime('%Y-%m-%dT%H:%M:%S', {m.group(1)}, '-' || {m.group(2)} || ' minutes')"
        if m.group(2).startswith(":")
        else f"strftime('%Y-%m-%dT%H:%M:%S', {m.group(1)}, '-{m.group(2)} minutes')",
        out,
    )

    # CONCAT(a, b, c) → (a || b || c)。SQLite に CONCAT は無い。BigQuery の CONCAT は
    # 引数に NULL があると NULL を返し、`||` も同じなので意味は変わらない（照会側は IFNULL 済み）。
    def _concat(m: re.Match) -> str:
        return "(" + " || ".join(_split_args(m.group(1))) + ")"

    while "CONCAT(" in out:
        out = re.sub(r"CONCAT\(((?:[^()]|\([^()]*\))*)\)", _concat, out, count=1)

    # TO_JSON_STRING(<ARRAY 列>) → その列そのもの。偽クライアントは REPEATED 列を
    # `json.dumps(list)` の文字列で持っており、BigQuery の TO_JSON_STRING(ARRAY<STRING>) と
    # 同じ表記（`["a","b"]`）になる（再送の重複排除の内容一致に使う）。
    out = re.sub(r"TO_JSON_STRING\(\s*(\w+)\s*\)", r"\1", out)

    # NULLIF(x, '') → SQLite にもある。翻訳不要だが、未対応で黙って通さないための覚え書き。

    for token in ("VECTOR_SEARCH", "ML.GENERATE_EMBEDDING", "ML.DISTANCE", "ARRAY_AGG",
                  "INFORMATION_SCHEMA"):
        if token in out.upper():
            raise UnsupportedSql(f"未対応の構文 {token} — 翻訳を足すか、この経路は別で検査する")

    flat: dict[str, Any] = {}
    for k, v in params.items():
        if isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                flat[f"{k}_{i}"] = item
            # UNNEST 以外の位置（INSERT の VALUES など）に配列がそのまま来る場合。
            # 配列列は JSON 文字列として持つ規約（_ARRAY_COLUMNS）に合わせる。
            flat[k] = json.dumps(list(v))
        elif isinstance(v, bool):
            flat[k] = int(v)
        elif hasattr(v, "isoformat"):        # datetime（TIMESTAMP パラメタ）
            flat[k] = v.isoformat()
        else:
            flat[k] = v
    return out, flat


class _Job:
    def __init__(self, rows: list[dict], affected: int = 0):
        self._rows = rows
        self.num_dml_affected_rows = affected

    def result(self):
        return iter(self._rows)


class _Table:
    def __init__(self, ref: str, schema: list | None = None):
        self.reference = ref
        self.schema = list(schema or [])


class FakeBigQueryClient:
    """`query` / write methods を持つ、行を返す偽クライアント。

    `tables` は ``{"proj.ds.terms": [ {列: 値}, ... ]}``。列名は本物の DDL に合わせること
    （合っていなければ SQL が落ちる = 検査として正しい）。
    """

    def __init__(self, tables: dict[str, list[dict]] | None = None) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        # BigQuery の SHA256 / TO_BASE64 を**本物の値**で評価する。ダイジェスト比較
        # （再送の重複排除）は「同じ入力で同じ値になる」ことを確かめたいので、
        # ここを偽の恒真にすると検査が飾りになる。
        self._conn.create_function(
            "SHA256", 1, lambda v: hashlib.sha256((v or "").encode()).digest()
        )
        self._conn.create_function(
            "TO_BASE64", 1, lambda v: base64.b64encode(v or b"").decode()
        )
        self.sqls: list[str] = []
        #: 渡されたクエリパラメータ（値つき）。**本物の BigQuery はジョブ設定として
        #: これを保存し、`jobs.get` が値ごと返す** ので、「何を照会に乗せたか」は
        #: SQL 本文と同じくらい検査に値する。
        self.query_params: list[dict[str, Any]] = []
        self.loads: list[tuple[str, list[dict]]] = []
        self._columns: dict[str, list[str]] = {}
        for ref, rows in (tables or {}).items():
            self.create(ref, rows)

    # --- 表の用意 ---
    def create(self, ref: str, rows: list[dict]) -> None:
        key = _table_key(ref)
        cols = sorted({c for r in rows for c in r})
        if not cols:
            raise ValueError(f"{ref}: 列が決まらない（1行以上の見本が要る）")
        self._columns[key] = cols
        self._conn.execute(f"CREATE TABLE {key} ({', '.join(cols)})")
        self._insert(key, rows)

    def _insert(self, key: str, rows: list[dict]) -> None:
        cols = self._columns[key]
        for r in rows:
            vals = []
            for c in cols:
                v = r.get(c)
                if isinstance(v, (list, tuple)) or c in _ARRAY_COLUMNS:
                    v = json.dumps(list(v or []))
                elif isinstance(v, bool):
                    v = int(v)
                vals.append(v)
            self._conn.execute(
                f"INSERT INTO {key} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                vals,
            )
        self._conn.commit()

    # --- BigQuery クライアントの面 ---
    def query(self, sql: str, job_config: Any = None) -> _Job:
        self.sqls.append(sql)
        params = {}
        for p in getattr(job_config, "query_parameters", None) or []:
            params[p.name] = getattr(p, "values", None) if hasattr(p, "values") else p.value
        self.query_params.append(dict(params))
        if sql.lstrip().upper().startswith("MERGE"):
            update_sql, insert_sql, _ = merge_to_upsert(sql)
            up_t, up_f = _translate(update_sql, params)
            cur = self._conn.execute(up_t, up_f)
            if cur.rowcount == 0:            # MATCHED しなかった = INSERT 側
                in_t, in_f = _translate(insert_sql, params)
                cur = self._conn.execute(in_t, in_f)
            self._conn.commit()
            return _Job([], affected=cur.rowcount)
        translated, flat = _translate(sql, params)
        cur = self._conn.execute(translated, flat)
        if translated.lstrip().upper().startswith(("UPDATE", "DELETE", "INSERT")):
            self._conn.commit()
            return _Job([], affected=cur.rowcount)
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            for c, v in list(d.items()):
                if c in _ARRAY_COLUMNS and isinstance(v, str):
                    d[c] = json.loads(v)
            rows.append(d)
        return _Job(rows)

    def insert_rows_json(self, table: str, rows: list[dict]) -> list:
        key = _table_key(table)
        if key not in self._columns:
            self.create(table, rows)
        else:
            self._insert(key, rows)
        return []

    def get_table(self, table: str) -> _Table:
        return _Table(table)

    def load_table_from_json(self, rows: list[dict], table: Any, job_config: Any = None) -> _Job:
        ref = str(getattr(table, "reference", table))
        self.loads.append((ref, [dict(row) for row in rows]))
        key = _table_key(ref)
        if key not in self._columns:
            self.create(ref, rows)
        else:
            self._insert(key, rows)
        return _Job([], affected=len(rows))
