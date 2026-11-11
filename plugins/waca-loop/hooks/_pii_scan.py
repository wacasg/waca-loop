#!/usr/bin/env python3
"""G5 の検出器。標準入力の本文を読み、当たった PII の**種別名だけ**を返す（値は返さない）。

正規表現は宣言しない — ``waca_loop.ingest.anonymize.detectors()`` を import して使う
（組織固有の会員番号の形式は org.toml の ``[pii] id_value_patterns`` から読まれる）
(docs/HOOKS_SPEC.md G5: 二重管理はドリフトの元)。本文は argv ではなく stdin で受け取る:
argv に置くと `ps` から他ユーザーに読まれる。
"""

import sys

from waca_loop.ingest.anonymize import detectors

text = sys.stdin.read()
print(",".join(sorted({kind for kind, pattern in detectors() if pattern.search(text)})))
