-- ベクトルインデックス — §2.3
-- 件数が少ないうちはブルートフォースでも可。COSINE/IVF を既定とする。
-- (VECTOR_SEARCH() works without an index on small tables; create this once terms grow.)
CREATE VECTOR INDEX IF NOT EXISTS terms_emb_idx
ON `<DS_GROUNDING>.terms`(embedding)
OPTIONS(distance_type = 'COSINE', index_type = 'IVF');
