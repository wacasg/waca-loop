-- OAuth sub → person_id マッピング（個人情報分離） — §5
-- Kept separate from `persons` so the subject↔person link is independently governed.
CREATE TABLE IF NOT EXISTS `<DS_GROUNDING>.oauth_identity_map` (
  oauth_sub   STRING NOT NULL,              -- OAuth subject (sub)
  person_id   STRING NOT NULL,              -- persons.person_id
  email       STRING,                       -- optional, from openid email scope
  created_at  TIMESTAMP NOT NULL,
  updated_at  TIMESTAMP NOT NULL,
  -- 監査痕跡 (§5). 事前登録行は本人の初回ログインで **その場で上書き** されるため、
  -- これが無いと事後には「管理者が sub を直接入れた行」と見分けが付かない。
  -- claimed_at は束ねられた時刻、claim_source は preregistration | admin。
  -- NULL は「経路が記録されていない」行であって admin ではない。
  claimed_at   TIMESTAMP,
  claim_source STRING
);
