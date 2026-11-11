-- Embedding model (§2.3 option b — bq_ml) — Vertex remote model used by
-- ML.GENERATE_EMBEDDING during backfill (scripts/backfill_embeddings.py) and query-time.
--
-- Prerequisites (run by the user; need gcloud/bq creds — see docs/SETUP_gcp.md):
--   1. Create a BigQuery -> Vertex connection (CLOUD_RESOURCE).
--   2. Grant the connection's service account roles/aiplatform.user.
-- <BQ_CONNECTION> is substituted from the BQ_CONNECTION env var by scripts/init_bigquery.py;
-- <DS_GROUNDING> from DS_GROUNDING. Japanese content -> multilingual endpoint.
CREATE MODEL IF NOT EXISTS `<DS_GROUNDING>.embedding_model`
REMOTE WITH CONNECTION `<BQ_CONNECTION>`
OPTIONS (ENDPOINT = 'text-multilingual-embedding-002');
