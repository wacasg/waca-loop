# WACA Loop 知識層アーキテクチャ(正典)

正典命題: **「知識の類型は固定する。データ源が増えても、増えるのは源泉コネクタ(コード)だけで、
テーブルは増えない。」**

```
┌─ 源泉層(staging / waca_loop_integration)────────────────────────────────┐
│  source_items(単一エンベロープテーブル — sql/ddl/09b_source_items.sql)│
│  コネクタ = 射影マップ＋許可列＋エンベロープ写像の3定数＋純粋関数        │
│  (源泉固有列は attrs JSON へ。源泉が増えてもテーブルは増えない)        │
└──────────────┬─────────────────────────────────────────────────┘
               │ 匿名化ゲート(列削除→決定的スクラブ・fail-closed・バイパス無し)
               │ ルーティング3層(層A規則 / 層B AI分類+confidence / 層C人。
               │ 振り分けの基準は組織が定める。route は最終判断ではない — FLOOR-03)
               ▼
┌─ 正典知識層(類型は固定 — これ以上増やさない)────────────────────────┐
│ ①用語 = terms                                                        │
│ ②意思決定＋実例＋FAQ = SFAQ(questions+feedback)                       │
│    実例: attributes.kind = "case"(INV-4: 固定列を増やさない)          │
│    FAQ: attributes.kind = "faq"(公開は将来の scope_audience ビュー)    │
│ ③人 = persons ＋ ERNシグナル(append-only・スコア列なし・集計導出)      │
│ ④操作 = usage_log                                                     │
│ (一般化された教訓を外部の事例システムで扱う場合は、ゲート通過後のデータ  │
│   だけを渡す。route = hold_external はその候補の保留)                   │
└──────────────┬─────────────────────────────────────────────────┘
               │ 帰属イベント(SFAQ昇格の人間承認 → handled_by へ adopt、
               │ feedback useful/agree → reference、refute → refute)
               ▼
出力面(テーブルではなくツール/ビュー):
  lookup_terms / search_sfaq / find_expert / FAQ公開ビュー / 統計
```

## 周辺層(知識ではない・類型の外で正当)

| 層 | 実体 | 役割 |
|---|---|---|
| 規範 | policy(レベル制 level 0-2+床行 is_floor) | 人が起案・承認する行。WACA Loop は配達係 |
| 計器 | survey_responses / improvement_tasks / tool_events | ループの健康測定(声・課題化・記録漏れ検知) |
| 配管 | oauth_identity_map / embedding_model / vector index | 認証・検索インフラ |
| バッファ | extraction_candidates | AI提案→人承認の一時置場。承認後は正典層へ消化 |
| 源泉 | source_items | 上図の入口。route 列が仕分け状態を持つ |

## 実例と教訓

| | 実例(instance) | 教訓(lesson) |
|---|---|---|
| 器 | SFAQ attributes.kind="case" | 外部の事例システム(任意) |
| 粒度 | この問いにこう対応した | 一般化された学び |
| 引き方 | 問い⇄問い(search_sfaq) | 意味検索・下流生成への引用 |
| ゲート | provisional→approved(人間) | 外部システム側で定める |

同じデータを二重登録しない。供給元はどちらも source_items(ゲート通過後)。

## ERN 連携の原則

ERN はコンテンツを持たない。シグナルは**源泉からではなく正典層への昇格・利用イベント**から発生する
(未解決の case の山で人物スコアが水増しされないため)。source_items は `handled_by`(person_id/slug —
生の氏名は入れない)を保持するだけでよく、承認済みの昇格を遡って append でバックフィルできる
(E7 append-only と整合)。

## コネクタ契約(新しい源泉を足すときの決まり)

1. `src/waca_loop/ingest/<source>.py` に3定数を置く: 射影マップ(仕様列名→実列名・ホワイトリスト)、
   エクスポート列契約(全列を固定し「写像∪破棄=全列・非交差」をテストで表明)、エンベロープ写像
   (core列 + attrs 対象)。
2. 写像の**値**にデナイリスト列名を置けない(import 時に失敗する)。人物列を持ち込む場合は、組織が明示的に
   承認した列だけを名指しで記録し(取り込みアダプタ側の許可リスト)、値は識別子化してからゲートに入れる(`handled_by`)。
3. ゲート・stamp・取り込み処理の fail-closed 前提条件は共通にする。
4. テーブルは作らない。源泉固有列は attrs JSON へ入れる。
