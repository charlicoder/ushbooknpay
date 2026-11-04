-- One-off repair of legacy payment attribution values.
-- Canonical: through ushweb|ushspa|ushdesk; provider myfatoorah|tap|knet_pos|boubyan;
-- method knet_card|apple_pay|deema_link|boubyan_link|gift_voucher. Unclear -> NULL.
-- Usage: psql -U postgres -d ushbooknpay_prod_db -f repair_payment_attribution.sql
BEGIN;

ALTER TABLE bookings ADD COLUMN IF NOT EXISTS branch_name VARCHAR(255);
ALTER TABLE payments ADD COLUMN IF NOT EXISTS branch_name VARCHAR(255);

-- Method first (it may depend on the raw provider value).
UPDATE bookings SET payment_method = CASE
  WHEN payment_method IS NULL THEN NULL
  WHEN LOWER(payment_method) IN ('on_branch','pay_on_branch','pay_now','unknown','other','') THEN NULL
  WHEN LOWER(payment_method) IN ('voucher','gift_voucher') THEN 'gift_voucher'
  WHEN LOWER(payment_method) LIKE '%apple%' THEN 'apple_pay'
  WHEN LOWER(payment_method) LIKE '%deema%' THEN 'deema_link'
  WHEN LOWER(payment_method) LIKE '%boubyan%' OR LOWER(COALESCE(payment_provider,'')) LIKE '%boubyan%' THEN 'boubyan_link'
  WHEN LOWER(COALESCE(payment_provider,'')) LIKE '%deema%' OR LOWER(COALESCE(payment_provider,'')) = 'tap' THEN 'deema_link'
  WHEN LOWER(payment_method) IN ('knet','knet_card','knetcard','card','knet card') THEN 'knet_card'
  ELSE payment_method END;

UPDATE payments SET payment_method = CASE
  WHEN payment_method IS NULL THEN NULL
  WHEN LOWER(payment_method) IN ('on_branch','pay_on_branch','pay_now','unknown','other','') THEN NULL
  WHEN LOWER(payment_method) IN ('voucher','gift_voucher') THEN 'gift_voucher'
  WHEN LOWER(payment_method) LIKE '%apple%' THEN 'apple_pay'
  WHEN LOWER(payment_method) LIKE '%deema%' THEN 'deema_link'
  WHEN LOWER(payment_method) LIKE '%boubyan%' OR LOWER(COALESCE(payment_provider,'')) LIKE '%boubyan%' THEN 'boubyan_link'
  WHEN LOWER(COALESCE(payment_provider,'')) LIKE '%deema%' OR LOWER(COALESCE(payment_provider,'')) = 'tap' THEN 'deema_link'
  WHEN LOWER(payment_method) IN ('knet','knet_card','knetcard','card','knet card') THEN 'knet_card'
  ELSE payment_method END;

-- Provider.
UPDATE bookings SET payment_provider = CASE
  WHEN payment_provider IS NULL THEN NULL
  WHEN LOWER(payment_provider) LIKE '%fatoorah%' OR LOWER(payment_provider) LIKE '%fatora%' THEN 'myfatoorah'
  WHEN LOWER(payment_provider) LIKE '%deema%' OR LOWER(payment_provider) = 'tap' THEN 'tap'
  WHEN LOWER(payment_provider) LIKE '%knet%' THEN 'knet_pos'
  WHEN LOWER(payment_provider) LIKE '%boubyan%' THEN 'boubyan'
  ELSE NULL END;
UPDATE payments SET payment_provider = CASE
  WHEN payment_provider IS NULL THEN NULL
  WHEN LOWER(payment_provider) LIKE '%fatoorah%' OR LOWER(payment_provider) LIKE '%fatora%' THEN 'myfatoorah'
  WHEN LOWER(payment_provider) LIKE '%deema%' OR LOWER(payment_provider) = 'tap' THEN 'tap'
  WHEN LOWER(payment_provider) LIKE '%knet%' THEN 'knet_pos'
  WHEN LOWER(payment_provider) LIKE '%boubyan%' THEN 'boubyan'
  ELSE NULL END;
UPDATE gift_vouchers SET payment_provider = CASE
  WHEN payment_provider IS NULL THEN NULL
  WHEN LOWER(payment_provider) LIKE '%fatoorah%' OR LOWER(payment_provider) LIKE '%fatora%' THEN 'myfatoorah'
  WHEN LOWER(payment_provider) LIKE '%deema%' OR LOWER(payment_provider) = 'tap' THEN 'tap'
  WHEN LOWER(payment_provider) LIKE '%knet%' THEN 'knet_pos'
  WHEN LOWER(payment_provider) LIKE '%boubyan%' THEN 'boubyan'
  ELSE NULL END;

-- Through.
UPDATE bookings SET payment_through = CASE
  WHEN LOWER(payment_through) IN ('desk','ushdesk') THEN 'ushdesk'
  WHEN LOWER(payment_through) IN ('web','ushweb') THEN 'ushweb'
  WHEN LOWER(payment_through) IN ('mobile','app','spa','ushspa') THEN 'ushspa'
  ELSE NULL END WHERE payment_through IS NOT NULL;
UPDATE payments SET payment_through = CASE
  WHEN LOWER(payment_through) IN ('desk','ushdesk') THEN 'ushdesk'
  WHEN LOWER(payment_through) IN ('web','ushweb') THEN 'ushweb'
  WHEN LOWER(payment_through) IN ('mobile','app','spa','ushspa') THEN 'ushspa'
  ELSE NULL END WHERE payment_through IS NOT NULL;
UPDATE gift_vouchers SET payment_through = CASE
  WHEN LOWER(payment_through) IN ('desk','ushdesk') THEN 'ushdesk'
  WHEN LOWER(payment_through) IN ('web','ushweb') THEN 'ushweb'
  WHEN LOWER(payment_through) IN ('mobile','app','spa','ushspa') THEN 'ushspa'
  ELSE NULL END WHERE payment_through IS NOT NULL;

-- Branch name denormalisation.
UPDATE bookings SET branch_name = COALESCE(branch_data->>'name', branch_data->>'branch_name')
  WHERE branch_name IS NULL AND branch_data IS NOT NULL;
UPDATE payments SET branch_name = COALESCE(branch_data->>'name', branch_data->>'branch_name')
  WHERE branch_name IS NULL AND branch_data IS NOT NULL;

COMMIT;

