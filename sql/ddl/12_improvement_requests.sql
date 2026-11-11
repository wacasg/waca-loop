-- ⑫ 改善要望 — WACA Loop という道具への注文 (docs/SPEC_improvement_requests.md §2)
-- <DS_FEEDBACK> is substituted from the DS_FEEDBACK env var by scripts/init_bigquery.py.
--
-- improvement_tasks とは別立て (§2)。あちらは申告・インシデント由来でスチュワードが捌く導線、
-- こちらは製品への要望で開発者が捌く導線。混ぜるとどちらの一覧も使い物にならなくなる。
--
-- **priority / severity の数値列を置かない** (§2・R6)。「最優先で提示」は AI が計算した
-- 優先度ではなく、未対応を他の話題より先に出す提示順のこと (§4)。数値列を作ると、
-- そのうち誰かがそれで人や部署を比べる。
--
-- **逐語を持たない** (INV-1)。body は症状と期待を書く欄で、プロンプトやログの貼り付け先ではない。

CREATE TABLE IF NOT EXISTS `<DS_FEEDBACK>.improvement_requests` (
  request_id     STRING NOT NULL,
  kind           STRING NOT NULL,          -- bug | improvement | other（「その他」は残す・§1-9）
  title          STRING NOT NULL,
  body           STRING NOT NULL,          -- 症状・期待・再現手順・動作環境を連結（§7.1）
  reporter_id    STRING,                   -- person_id。匿名（名前空欄）なら NULL（§1-7）
  reporter_label STRING,                   -- 表示名 or "匿名"
  channel        STRING NOT NULL,          -- mcp | form
  status         STRING NOT NULL,          -- unverified|open|designing|implemented|released|wontfix
  owner_unit     STRING,                   -- resolve_owner_unit 経由。推測しない（§3.1）
  -- 3軸スコープ。ほかのレコード種別と同じ列名にして、retrieval/acl.acl_where_clause を
  -- そのまま使えるようにしてある（要望専用の ACL 方言を作らない）。
  scope_tier        STRING NOT NULL,
  scope_audience    STRING NOT NULL,
  scope_sensitivity STRING NOT NULL,
  issue_url      STRING,                   -- 管理者が押したあとに埋まる（§1-2）
  resolution     STRING,                   -- 実装内容の要約。シートへ書き戻す本文
  created_at     TIMESTAMP NOT NULL,
  updated_at     TIMESTAMP NOT NULL
);
