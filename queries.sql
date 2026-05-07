-- ═══════════════════════════════════════════════════════════════════
-- NYC Street Vendor Intelligence — Analytical Queries
-- Database: data/clean/nyc_vendors.db (SQLite)
-- Tables: violations, commissaries, parks_eateries
-- ═══════════════════════════════════════════════════════════════════
-- Usage: These queries are auto-loaded by clean_and_analyze.py.
--        They can also be run standalone:
--          sqlite3 data/clean/nyc_vendors.db < queries.sql
-- ═══════════════════════════════════════════════════════════════════

-- @name: Top 10 vendors by cumulative fines paid
SELECT full_name,
       COUNT(*)                         AS violations,
       violation_location_borough       AS borough,
       ROUND(SUM(paid_amount_clean), 2) AS total_paid,
       MAX(violation_date)              AS latest
FROM   violations
WHERE  LENGTH(full_name) >= 3
GROUP  BY full_name
ORDER  BY total_paid DESC
LIMIT  10;

-- @name: Violations per borough per year (trend)
SELECT violation_location_borough AS borough,
       SUBSTR(violation_date, 1, 4) AS year,
       COUNT(*) AS violations
FROM   violations
WHERE  violation_date IS NOT NULL
  AND  violation_location_borough IS NOT NULL
GROUP  BY borough, year
ORDER  BY borough, year;

-- @name: Vendor tier distribution (active since 2020)
-- Tiers: Platinum ≥40, Gold ≥25, Silver ≥15, Bronze <15 violations
SELECT CASE
         WHEN cnt >= 40 THEN 'Platinum'
         WHEN cnt >= 25 THEN 'Gold'
         WHEN cnt >= 15 THEN 'Silver'
         ELSE 'Bronze'
       END AS tier,
       COUNT(*)               AS vendors,
       ROUND(AVG(cnt), 1)     AS avg_violations,
       ROUND(SUM(total_paid), 2) AS sum_paid
FROM (
    SELECT full_name,
           COUNT(*)                AS cnt,
           SUM(paid_amount_clean)  AS total_paid,
           MAX(violation_date)     AS latest
    FROM   violations
    WHERE  LENGTH(full_name) >= 3
    GROUP  BY full_name
    HAVING latest >= '2020-01-01'
)
GROUP  BY tier
ORDER  BY avg_violations DESC;

-- @name: Commissaries per borough (hub density)
SELECT borough,
       COUNT(*) AS hubs,
       SUM(CASE WHEN phone IS NOT NULL AND phone != '' THEN 1 ELSE 0 END) AS with_phone
FROM   commissaries
GROUP  BY borough
ORDER  BY hubs DESC;

-- @name: Vendors per commissary hub (borough-level ratio)
-- Operational question: how many vendors would each hub serve?
SELECT c.borough,
       c.hubs               AS commissaries,
       v.vendors,
       ROUND(CAST(v.vendors AS FLOAT) / c.hubs, 1) AS vendors_per_hub
FROM (
    SELECT borough, COUNT(*) AS hubs
    FROM   commissaries
    GROUP  BY borough
) c
LEFT JOIN (
    SELECT violation_location_borough AS borough,
           COUNT(DISTINCT full_name) AS vendors
    FROM   violations
    WHERE  LENGTH(full_name) >= 3
    GROUP  BY violation_location_borough
) v ON c.borough = v.borough
ORDER  BY vendors_per_hub DESC;

-- @name: Monthly violation trend (seasonality check)
-- Do violations spike in summer (peak vending season)?
SELECT SUBSTR(violation_date, 6, 2) AS month,
       COUNT(*)                     AS violations,
       ROUND(AVG(paid_amount_clean), 2) AS avg_paid
FROM   violations
WHERE  violation_date IS NOT NULL
GROUP  BY month
ORDER  BY month;

-- @name: Top charge codes (what are vendors actually cited for?)
-- This reveals which violations CartZero could help reduce.
SELECT "charge_1:_code_description" AS charge,
       COUNT(*) AS occurrences,
       ROUND(AVG(paid_amount_clean), 2) AS avg_fine
FROM   violations
WHERE  "charge_1:_code_description" IS NOT NULL
  AND  "charge_1:_code_description" != ''
GROUP  BY charge
ORDER  BY occurrences DESC
LIMIT  15;
