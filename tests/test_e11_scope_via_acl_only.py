"""E11 — スコープ判定は acl.py 経由のみ（§3.3）.

ERN のモジュールに tier / audience / 三色語彙の文字列比較を直書きしない。grep 表明。

理由は「二重ゲートを2箇所に書かない」と同じ。可視性の判定がコピーされると、片方だけ直る。
`retrieval/acl.py` が唯一の場所であることを、**新しいモジュールが増えたときにも**保つ。
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil

import waca_loop.ern
from waca_loop.ern import engine, store


def _all_ern_modules():
    """`waca_loop.ern.*` を**自動列挙**する.

    手書きの一覧にすると、新しく足したモジュール（`lifecycle.py` など）が最初から漏れる。
    この検査の docstring 自身が「新しいモジュールが増えたときにも保つ」と言っているので、
    増えたら自動で対象に入る形にする。
    """
    mods = [waca_loop.ern]
    for info in pkgutil.iter_modules(waca_loop.ern.__path__):
        mods.append(importlib.import_module(f"waca_loop.ern.{info.name}"))
    return tuple(mods)


MODULES = _all_ern_modules()

#: スコープ語彙。これらの**文字列リテラル**が ERN のコードに現れてはいけない。
SCOPE_VOCAB = {
    "personal", "team", "dept", "org",
    "internal", "partner", "public",
    "normal", "sensitive",
    "S0", "S1", "S2", "S3", "S4", "S5",
    # 三色語彙
    "赤", "黄", "青", "red", "yellow", "green",
}


def _string_constants(mod) -> set[str]:
    """コードに現れる文字列リテラル（docstring は除く）.

    docstring を除くのが要点 — 「なぜ dept を直書きしないか」を説明した文が引っかかると、
    説明を消すインセンティブになる。
    """
    tree = ast.parse(inspect.getsource(mod))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    return {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value not in docstrings
    }


def test_the_module_list_is_not_hand_written():
    """C(負): 対象が自動列挙されている（新しいモジュールが黙って検査外にならない）."""
    names = {m.__name__.rsplit(".", 1)[-1] for m in MODULES}
    assert {"engine", "schema", "signals", "store", "weights", "lifecycle"} <= names, names


def test_no_scope_vocabulary_is_hardcoded():
    """C(負): ERN のどのモジュールにもスコープ語彙のリテラルが無い."""
    for mod in MODULES:
        found = _string_constants(mod) & SCOPE_VOCAB
        assert not found, f"{mod.__name__} にスコープ語彙の直書き: {sorted(found)}"


def test_visibility_comes_from_acl_functions():
    """A(正): 可視性は `retrieval.acl` / `knowledge.acl` の関数呼び出しで得ている."""
    engine_src = inspect.getsource(engine)
    assert "person_visible_to" in engine_src
    assert "mask_author" in engine_src

    store_src = inspect.getsource(store)
    assert "person_visible_to" in store_src
    assert "person_where_clause" in store_src


def _identifiers(mod) -> set[str]:
    """コードに現れる名前・属性名（**説明文は含まない**）.

    文字列検索にすると、「なぜ consent_internal を直接見ないか」を説明した docstring が
    引っかかる。ルールを説明した文を消すインセンティブが生まれるので、AST で見る。
    """
    tree = ast.parse(inspect.getsource(mod))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


def test_the_ern_modules_do_not_reimplement_the_gate():
    """C(負): 判定式そのものを写経していない（コードとして触っていない）."""
    forbidden = {"consent_internal", "consent_external", "scope_tier", "scope_audience",
                 "scope_sensitivity", "allowed_sensitive_units"}
    for mod in MODULES:
        found = _identifiers(mod) & forbidden
        assert not found, f"{mod.__name__} がゲートを写経している: {sorted(found)}"


def test_e12a_ern_never_imports_circles():
    """E12(a): circles を import しない（配線しないことを構造で示す）.

    将来配線しても3軸に AND で劣後させる — サークル共有を理由に可視範囲を広げない。
    いまは「触っていない」ことが、その約束の一番強い形。
    """
    for mod in MODULES:
        assert "circles" not in _identifiers(mod), f"{mod.__name__} が circles に触れている"
