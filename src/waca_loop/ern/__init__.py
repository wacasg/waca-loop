"""ERN — 専門度・貢献量・信頼度エンジン（SPEC_phase3）.

**評価ではなくマッチング**（INV-5）。ここが崩れると、組織の中に「人を採点する装置」が
できる。だから3エンジンの計算結果は、いずれも**外部に数値としては出ない** — 出るのは
band（帯）と `evidence_type` だけ（SPEC_phase3 §2.3 注）。

モジュールの分け方も、その一線を守るためにある:

- :mod:`~waca_loop.ern.weights` — 係数を1箇所に集める（ロジック内リテラル禁止・E9）
- :mod:`~waca_loop.ern.schema`  — 出力の許可リスト関門（E1）。全 ERN ツールに掛ける
- :mod:`~waca_loop.ern.signals` — `feedback` の既存行 → シグナル（新テーブルを作らない）
- :mod:`~waca_loop.ern.engine`  — 3エンジンと候補選定
- :mod:`~waca_loop.ern.store`   — `ern_assertions` / `ern_audit` の InMemory / BigQuery 対

E11: このパッケージのどのモジュールも、tier / audience / 三色語彙の文字列比較を直書きしない。
可視性の判定は `waca_loop.retrieval.acl` / `waca_loop.knowledge.acl` の関数呼び出しに限る。
E12(a): `circles` を import しない。
"""
