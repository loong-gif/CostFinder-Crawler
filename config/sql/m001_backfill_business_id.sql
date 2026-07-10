-- Migration M001: 回填 promo_offer_master.business_id NULL 值
-- 通过 source_url 域名匹配 master_business_info.website_clean

-- 先预览匹配情况
SELECT '=== BEFORE ===' as step;
SELECT COUNT(*) as total, COUNT(business_id) as with_biz, COUNT(*) - COUNT(business_id) as null_biz
FROM promo_offer_master;

-- 执行回填：域名去www.后匹配
UPDATE promo_offer_master po
SET business_id = mb.business_id
FROM master_business_info mb
WHERE po.business_id IS NULL
  AND po.source_url IS NOT NULL
  AND REPLACE(SPLIT_PART(SPLIT_PART(po.source_url, '://', 2), '/', 1), 'www.', '')
      = REPLACE(mb.website_clean, 'www.', '');

SELECT '=== AFTER ===' as step;
SELECT COUNT(*) as total, COUNT(business_id) as with_biz, COUNT(*) - COUNT(business_id) as null_biz
FROM promo_offer_master;
