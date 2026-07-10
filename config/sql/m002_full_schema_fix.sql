-- ============================================================================
-- Migration: M002_full_schema_fix
-- 根据 Schema 审阅报告 (2026-07-09) 的 P0/P1/P2 问题分级全面修复
-- 
-- 执行前注意事项:
--   1. 在 Supabase Dashboard → SQL Editor 中执行
--   2. 建议先在事务中跑一遍，确认无错误再提交
--   3. P0-1 已在脚本 backfill_business_id_v2.sh 中执行（2955条已回填）
--   4. 本迁移假设 P0-1 已部分执行
--
-- 执行方式:
--   Supabase Dashboard → SQL Editor → 粘贴全部 → 运行
--   或: python scripts/apply_sql_migration.py config/sql/m002_full_schema_fix.sql
-- ============================================================================

BEGIN;

-- ============================================================================
-- P0: 数据完整性修复
-- ============================================================================

-- ---------------------------------------------------------------------------
-- P0-1: 补充回填剩余的 business_id (339条)
-- ---------------------------------------------------------------------------
-- 通过 source_url 域名匹配 master_business_info.website_clean（含子域名）
UPDATE promo_offer_master po
SET business_id = mb.business_id
FROM master_business_info mb
WHERE po.business_id IS NULL
  AND po.source_url IS NOT NULL
  AND (
    -- 精确域名匹配（去www.）
    REPLACE(SPLIT_PART(SPLIT_PART(po.source_url, '://', 2), '/', 1), 'www.', '')
    = REPLACE(mb.website_clean, 'www.', '')
    OR
    -- 子域名匹配：lp.h-md.com → h-md.com
    RIGHT(REPLACE(SPLIT_PART(SPLIT_PART(po.source_url, '://', 2), '/', 1), 'www.', ''),
          LENGTH(REPLACE(mb.website_clean, 'www.', '')) + 1)
    = '.' || REPLACE(mb.website_clean, 'www.', '')
  );

-- 从 business_list_production 补充匹配（URL格式完整）
UPDATE promo_offer_master po
SET business_id = bp.business_id
FROM business_list_production bp
WHERE po.business_id IS NULL
  AND po.source_url IS NOT NULL
  AND bp.website IS NOT NULL
  AND SPLIT_PART(po.source_url, '?', 1) = SPLIT_PART(bp.website, '?', 1);

SELECT 'P0-1: remaining NULL business_id = ' || COUNT(*)::text
FROM promo_offer_master WHERE business_id IS NULL;

-- ---------------------------------------------------------------------------
-- P0-2: 添加缺失的 FK 约束
-- ---------------------------------------------------------------------------
-- 注意: 添加前确保数据已清理完毕，否则会报错

-- promo_offer_master.business_id → master_business_info.business_id
ALTER TABLE promo_offer_master
  ADD CONSTRAINT fk_promo_offer_master_business
  FOREIGN KEY (business_id) REFERENCES master_business_info(business_id)
  ON DELETE SET NULL;

-- promo_offer_master.membership_plan_id → promo_membership_plans.plan_id
ALTER TABLE promo_offer_master
  ADD CONSTRAINT fk_promo_offer_master_plan
  FOREIGN KEY (membership_plan_id) REFERENCES promo_membership_plans(plan_id)
  ON DELETE SET NULL;

-- promo_membership_plans.business_id → master_business_info.business_id
ALTER TABLE promo_membership_plans
  ADD CONSTRAINT fk_promo_membership_plans_business
  FOREIGN KEY (business_id) REFERENCES master_business_info(business_id)
  ON DELETE CASCADE;

-- promo_membership_plans.promo_website_id → promo_website_staging.promo_website_id
ALTER TABLE promo_membership_plans
  ADD CONSTRAINT fk_promo_membership_plans_website
  FOREIGN KEY (promo_website_id) REFERENCES promo_website_staging(promo_website_id)
  ON DELETE SET NULL;

-- promo_website_staging.business_id → master_business_info.business_id
ALTER TABLE promo_website_staging
  ADD CONSTRAINT fk_promo_website_staging_business
  FOREIGN KEY (business_id) REFERENCES master_business_info(business_id)
  ON DELETE SET NULL;

-- promo_email_staging.business_id → master_business_info.business_id
ALTER TABLE promo_email_staging
  ADD CONSTRAINT fk_promo_email_staging_business
  FOREIGN KEY (business_id) REFERENCES master_business_info(business_id)
  ON DELETE SET NULL;

-- promo_social_staging.business_id → master_business_info.business_id
ALTER TABLE promo_social_staging
  ADD CONSTRAINT fk_promo_social_staging_business
  FOREIGN KEY (business_id) REFERENCES master_business_info(business_id)
  ON DELETE SET NULL;

-- social_data_production: 旧数据不再使用，清空后由业务重新填充
TRUNCATE social_data_production;
-- social_data_staging: 已无业务需要，直接移除
DROP TABLE IF EXISTS social_data_staging;

-- claims.consumer_id → profiles.id
ALTER TABLE claims
  ADD CONSTRAINT fk_claims_consumer
  FOREIGN KEY (consumer_id) REFERENCES profiles(id)
  ON DELETE CASCADE;

-- claims.deal_id → promo_offer_master.id
ALTER TABLE claims
  ADD CONSTRAINT fk_claims_deal
  FOREIGN KEY (deal_id) REFERENCES promo_offer_master(id)
  ON DELETE CASCADE;

-- claims.business_id → master_business_info.business_id
ALTER TABLE claims
  ADD CONSTRAINT fk_claims_business
  FOREIGN KEY (business_id) REFERENCES master_business_info(business_id)
  ON DELETE CASCADE;

-- saved_deals.deal_id → promo_offer_master.id
ALTER TABLE saved_deals
  ADD CONSTRAINT fk_saved_deals_deal
  FOREIGN KEY (deal_id) REFERENCES promo_offer_master(id)
  ON DELETE CASCADE;

-- business_claims.business_id → master_business_info.business_id
ALTER TABLE business_claims
  ADD CONSTRAINT fk_business_claims_business
  FOREIGN KEY (business_id) REFERENCES master_business_info(business_id)
  ON DELETE CASCADE;

-- ---------------------------------------------------------------------------
-- P0-3: 为 FK 列创建索引
-- ---------------------------------------------------------------------------

-- promo_offer_master 高频查询索引
CREATE INDEX IF NOT EXISTS idx_promo_offer_master_business_id
  ON promo_offer_master(business_id);
CREATE INDEX IF NOT EXISTS idx_promo_offer_master_membership_plan_id
  ON promo_offer_master(membership_plan_id);

-- 按城市+品类查询最优索引（比价网站核心查询）
CREATE INDEX IF NOT EXISTS idx_master_business_info_city
  ON master_business_info(city);
CREATE INDEX IF NOT EXISTS idx_promo_offer_master_active_category
  ON promo_offer_master(service_category, business_id)
  WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_promo_offer_master_active_price
  ON promo_offer_master(discount_price ASC)
  WHERE status = 'active';

-- staging 表 FK 索引
CREATE INDEX IF NOT EXISTS idx_promo_website_staging_business_id
  ON promo_website_staging(business_id);
CREATE INDEX IF NOT EXISTS idx_promo_email_staging_business_id
  ON promo_email_staging(business_id);
CREATE INDEX IF NOT EXISTS idx_promo_social_staging_business_id
  ON promo_social_staging(business_id);
CREATE INDEX IF NOT EXISTS idx_promo_membership_plans_business_id
  ON promo_membership_plans(business_id);
CREATE INDEX IF NOT EXISTS idx_promo_membership_plans_website_id
  ON promo_membership_plans(promo_website_id);

-- social 索引
CREATE INDEX IF NOT EXISTS idx_social_data_production_business_id
  ON social_data_production(business_id);
CREATE INDEX IF NOT EXISTS idx_social_data_staging_business_id
  ON social_data_staging(business_id);

-- claims/saved_deals 索引
CREATE INDEX IF NOT EXISTS idx_claims_consumer_id
  ON claims(consumer_id);
CREATE INDEX IF NOT EXISTS idx_claims_deal_id
  ON claims(deal_id);
CREATE INDEX IF NOT EXISTS idx_claims_business_id
  ON claims(business_id);
CREATE INDEX IF NOT EXISTS idx_saved_deals_deal_id
  ON saved_deals(deal_id);
CREATE INDEX IF NOT EXISTS idx_business_claims_business_id
  ON business_claims(business_id);

-- ============================================================================
-- P1: 类型统一与列修正
-- ============================================================================

-- ---------------------------------------------------------------------------
-- P1-2: json → jsonb (3 处)
-- ---------------------------------------------------------------------------
ALTER TABLE master_business_info
  ALTER COLUMN membership TYPE jsonb
  USING membership::jsonb;

ALTER TABLE promo_website_staging
  ALTER COLUMN membership_context TYPE jsonb
  USING membership_context::jsonb;

-- ---------------------------------------------------------------------------
-- P1-3: promo_offer_master 列类型修正
-- ---------------------------------------------------------------------------

-- text → boolean
ALTER TABLE promo_offer_master
  ALTER COLUMN is_package TYPE boolean
  USING CASE WHEN is_package IS NULL THEN NULL
             WHEN LOWER(is_package) IN ('true', 't', 'yes', '1') THEN true
             ELSE false END;

ALTER TABLE promo_offer_master
  ALTER COLUMN is_membership_required TYPE boolean
  USING CASE WHEN is_membership_required IS NULL THEN NULL
             WHEN LOWER(is_membership_required) IN ('true', 't', 'yes', '1') THEN true
             ELSE false END;

-- text → date
-- 先检查 end_date 格式
DO $$
DECLARE
  bad_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO bad_count
  FROM promo_offer_master
  WHERE end_date IS NOT NULL
    AND end_date !~ '^\d{4}-\d{2}-\d{2}$';

  IF bad_count > 0 THEN
    RAISE NOTICE 'Found % rows with non-date end_date values, setting to NULL', bad_count;
    UPDATE promo_offer_master SET end_date = NULL
    WHERE end_date IS NOT NULL AND end_date !~ '^\d{4}-\d{2}-\d{2}$';
  END IF;
END $$;

ALTER TABLE promo_offer_master
  ALTER COLUMN end_date TYPE date
  USING CASE WHEN end_date IS NULL THEN NULL ELSE end_date::date END;

-- real → numeric(10,2) for price fields
ALTER TABLE promo_offer_master
  ALTER COLUMN regular_price TYPE numeric(10,2)
  USING CASE WHEN regular_price IS NULL THEN NULL ELSE regular_price::numeric END;

ALTER TABLE promo_offer_master
  ALTER COLUMN discount_price TYPE numeric(10,2)
  USING CASE WHEN discount_price IS NULL THEN NULL ELSE discount_price::numeric END;

-- text → numeric for amount/price fields
ALTER TABLE promo_offer_master
  ALTER COLUMN discount_amount TYPE numeric(10,2)
  USING CASE WHEN discount_amount IS NULL THEN NULL
             WHEN discount_amount ~ '^\d+(\.\d+)?$' THEN discount_amount::numeric
             ELSE NULL END;

ALTER TABLE promo_offer_master
  ALTER COLUMN membership_price TYPE numeric(10,2)
  USING CASE WHEN membership_price IS NULL THEN NULL
             WHEN membership_price ~ '^\d+(\.\d+)?$' THEN membership_price::numeric
             ELSE NULL END;

-- ---------------------------------------------------------------------------
-- P1-4: master_business_info 结构优化
-- ---------------------------------------------------------------------------

-- 拆分 process_flag 为明确的字段
-- 当前值: 'filtered', '2026-03-08', '2026-04-03'
ALTER TABLE master_business_info
  ADD COLUMN IF NOT EXISTS is_filtered boolean DEFAULT false,
  ADD COLUMN IF NOT EXISTS last_processed_at timestamptz;

UPDATE master_business_info
SET
  is_filtered = (process_flag = 'filtered'),
  last_processed_at = CASE
    WHEN process_flag ~ '^\d{4}-\d{2}-\d{2}$' THEN process_flag::timestamptz
    ELSE NULL
  END;

-- 注意: process_flag 列暂保留以兼容旧代码，后续可移除
COMMENT ON COLUMN master_business_info.process_flag IS 'DEPRECATED: use is_filtered + last_processed_at instead';

-- ---------------------------------------------------------------------------
-- P1-5: 将 business_list_production 转换为 master_business_info 的视图
-- 或将未进入 master 表的数据插入
-- ---------------------------------------------------------------------------
INSERT INTO master_business_info (business_id, name, address, website, review_count, score, category)
SELECT bp.business_id, bp.name, bp.address, bp.website, bp.review_count, bp.score, bp.category
FROM business_list_production bp
WHERE bp.business_id NOT IN (SELECT business_id FROM master_business_info)
ON CONFLICT (business_id) DO NOTHING;

-- 创建同名视图以兼容旧代码（如果业务允许）
-- CREATE OR REPLACE VIEW business_list_production AS
-- SELECT * FROM master_business_info WHERE ...;

-- ============================================================================
-- P2: 架构优化
-- ============================================================================

-- ---------------------------------------------------------------------------
-- P2-2: updated_at 自动更新触发器（通用）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 为所有有 updated_at 列的表创建触发器
DO $$
DECLARE
  tbl TEXT;
BEGIN
  FOR tbl IN
    SELECT table_name FROM information_schema.columns
    WHERE column_name = 'updated_at' AND table_schema = 'public'
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS trg_%s_updated_at ON %I;', tbl, tbl
    );
    EXECUTE format(
      'CREATE TRIGGER trg_%s_updated_at BEFORE UPDATE ON %I
       FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();',
      tbl, tbl
    );
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- P2-3: 软删除机制（增加 deleted_at）
-- ---------------------------------------------------------------------------
-- 为主表增加软删除列
ALTER TABLE promo_offer_master
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE master_business_info
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE promo_membership_plans
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

-- 索引：排除已删除的优惠（比价查询不用显示已删除）
CREATE INDEX IF NOT EXISTS idx_promo_offer_master_active
  ON promo_offer_master(business_id, service_category)
  WHERE status = 'active' AND deleted_at IS NULL;

-- ---------------------------------------------------------------------------
-- P2-6: 向量搜索索引 (HNSW)
-- ---------------------------------------------------------------------------
-- master_business_info.embedding 是 vector(1536) 类型
-- Pgvector 扩展必须已启用
CREATE INDEX IF NOT EXISTS idx_master_business_info_embedding_hnsw
  ON master_business_info
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- ============================================================================
-- 验证
-- ============================================================================
SELECT '=== M002 Schema Fix Complete ===' as status;

-- 汇总变更
SELECT 'FK added: promo_offer_master.business_id, promo_offer_master.membership_plan_id, promo_membership_plans.business_id, promo_membership_plans.promo_website_id, promo_website_staging.business_id, promo_email_staging.business_id, promo_social_staging.business_id, social_data_production.business_id, social_data_staging.business_id, claims.*, saved_deals.deal_id, business_claims.business_id' as fk_changes;

SELECT 'Indexes: ' || COUNT(*)::text || ' created or verified'
FROM pg_indexes WHERE schemaname = 'public' AND indexname LIKE 'idx\_%';

SELECT 'json→jsonb: membership(master_business_info), membership_context(promo_website_staging)' as jsonb_changes;

SELECT 'price precision: regular_price, discount_price → numeric(10,2); discount_amount, membership_price → numeric(10,2)' as numeric_changes;

SELECT 'boolean: is_package, is_membership_required' as bool_changes;

SELECT 'soft delete: promo_offer_master.deleted_at, master_business_info.deleted_at, promo_membership_plans.deleted_at' as soft_delete;

COMMIT;
