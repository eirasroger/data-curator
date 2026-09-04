-- The EPD store.
--
-- APPEND-ONLY, and versioned. Applying a change writes a NEW row with
-- version+1; it never updates the old one. Three reasons that matters here:
--
--   1. Compliance. If someone cited a GWP figure in a report last March, you
--      have to be able to show what the record said in March. An UPDATE
--      destroys that answer forever.
--   2. BigQuery has no primary keys and no unique constraints. Enforcing "one
--      row per EPD" on write means a MERGE per change, which is a whole query
--      per row, or a read-then-write that races itself.
--   3. Replaying a dead-lettered message becomes safe by construction: it just
--      writes another version.
--
-- The cost of append-only is that "the current record" is a query, not a table.
-- That is what the view below is for.

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

  -- the handful of fields worth querying directly. Everything else lives in
  -- `record`: promoting all ~40 schema fields to columns would mean a schema
  -- migration every time the extractor learns a new field.
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
-- No PARTITION BY: partitioning pays off when a table is large enough that
-- pruning whole days of data saves real money. At 63 products it would only add
-- metadata. change_events, which grows without limit, is partitioned.


-- What "the EPD record" means today: the highest version of each product.
CREATE OR REPLACE VIEW `curator.epd_current` AS
SELECT * EXCEPT(_rn)
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY version DESC) AS _rn
  FROM `curator.epd_records`
)
WHERE _rn = 1;


-- Expiry, as a question you can ask rather than a flag someone has to remember
-- to set. Derived from the date, so it is right even if the nightly job has not
-- run yet.
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
