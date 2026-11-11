-- OAuth DCR 登録クライアントの永続化
-- claude.ai は /register で得た client_id を保存し続けるため、インスタンス再起動で
-- 登録が消えると「再接続」も失敗する。ここに永続化して再起動を生き延びさせる。
-- client_json は SDK の OAuthClientInformationFull（client_secret を含み得る＝機微。
-- oauth_identity_map と同じ制限付きデータセットに置き、ログに書かない）。
-- 書き込みは insert-only（streaming）。読み出しは client_id ごとに最新行を採用。
-- 注意: /register は仕様上（DCR, RFC 7591）未認証で公開されているため、匿名の連続登録が
-- 行増加になり得る（監視: 利用者数に比べて行数が大きく増えたら調査する）。
-- 未知 client_id の /token 連打は client_store.py のネガティブキャッシュで、/register の
-- 連続登録は as_provider.py のレート制限（30件/時）で BQ を守る。
-- これはインフラテーブルであり §2.1 の3軸スコープ列を持たない — grounding 検索に JOIN
-- してはならず、viewer 向けクエリから参照してはならない（auth 経路専用）。
CREATE TABLE IF NOT EXISTS `<DS_GROUNDING>.oauth_clients` (
  client_id   STRING NOT NULL,              -- DCR-issued client identifier (uuid)
  client_json STRING NOT NULL,              -- serialized OAuthClientInformationFull
  created_at  TIMESTAMP NOT NULL
);
