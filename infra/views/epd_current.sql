SELECT * EXCEPT(_rn)
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY version DESC) AS _rn
  FROM `${dataset}.epd_records`
)
WHERE _rn = 1
