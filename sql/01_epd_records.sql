-- EPD records, append-only: each applied change adds a row with version + 1.
-- The epd_current view below returns the latest version of each product.

CREATE TABLE IF NOT EXISTS `curator.epd_records`
(
  product_id        INT64  NOT NULL,
  version           INT64  NOT NULL OPTIONS(description="1 for the seeded record, +1 per applied change"),
  valid_from        TIMESTAMP NOT NULL OPTIONS(description="when this version was written"),
  change_request_id STRING OPTIONS(description="the request that produced this version; null for the seed"),

  -- identity
  epd_code          STRING,
  prod_name         STRING,
  prod_man          STRING,
  prod_site         STRING,

  -- lifecycle
  expiry_date       DATE   OPTIONS(description="the EPD's 'valid until' date, NOT its publication date"),
  status            STRING NOT NULL OPTIONS(description="active | expired | superseded"),

  -- Frequently queried fields; the full record is in `record`.
  reference_unit    STRING,
  density           FLOAT64,
  thickness         FLOAT64,
  lifespan          FLOAT64,
  gwp_total         FLOAT64,
  gwp_fossil        FLOAT64,
  gwp_luluc         FLOAT64,
  gwp_bio           FLOAT64,

  record            JSON   NOT NULL OPTIONS(description="the complete EPDProduct as extracted")
)
CLUSTER BY product_id
OPTIONS(description="Versioned EPD records. Query epd_current for the latest of each.");
-- Unpartitioned: the table is small.


-- What "the EPD record" means today: the highest version of each product.
CREATE OR REPLACE VIEW `curator.epd_current` AS
SELECT * EXCEPT(_rn)
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY version DESC) AS _rn
  FROM `curator.epd_records`
)
WHERE _rn = 1;


-- Expiry state derived from the date, independent of the nightly job.
CREATE OR REPLACE VIEW `curator.epd_expiry_status` AS
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
FROM `curator.epd_current`
WHERE status != 'superseded';
