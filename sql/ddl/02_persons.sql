-- ② 人物テーブル（用語とは別。個人情報として分離） — §2.2 ②
CREATE TABLE IF NOT EXISTS `<DS_GROUNDING>.persons` (
  person_id         STRING NOT NULL,        -- UUID（OAuth subとは別。マッピングは別管理）
  display_name      STRING NOT NULL,
  unit_path         ARRAY<STRING>,          -- 所属（複数可・薄いメンバーシップ）
  consent_internal  BOOL NOT NULL,          -- 社内エキスパート探索 既定true（オプトアウト）
  consent_external  BOOL NOT NULL,          -- 社外公開 既定false（オプトイン）
  circles           ARRAY<STRING>,          -- 所属サークルID（拡張用）
  status            STRING NOT NULL,        -- active|frozen（退職時frozen→1年でERN側削除）
  created_at        TIMESTAMP NOT NULL,
  updated_at        TIMESTAMP NOT NULL
);
