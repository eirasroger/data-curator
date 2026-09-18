SELECT
  product_id,
  epd_code,
  prod_name,
  prod_man,
  expiry_date,
  DATE_DIFF(expiry_date, CURRENT_DATE(), DAY) AS days_until_expiry,
  CASE
    WHEN expiry_date IS NULL                                     THEN 'no expiry date'
    WHEN expiry_date <  CURRENT_DATE()                           THEN 'expired'
    WHEN DATE_DIFF(expiry_date, CURRENT_DATE(), DAY) <= 90       THEN 'expiring within 90 days'
    WHEN DATE_DIFF(expiry_date, CURRENT_DATE(), DAY) <= 365      THEN 'expiring within a year'
    ELSE 'valid'
  END AS expiry_state
FROM `${dataset}.epd_current`
WHERE status != 'superseded'
