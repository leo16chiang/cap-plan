-- Monthly usage for ONE application, read by usage.py when PORTAL_USAGE=db2.
-- Edit the table and column names to match your Db2 table. Keep the two output columns in this order:
--   1. the month (DATE/TIMESTAMP, 'YYYY-MM' text, or a YYYYMM number)
--   2. the average MSU for that month
-- The single ? is the application's usage_key from sample_data/teams.json.
-- The app needs at least 24 months for the year-over-year figure and the statistical projection.
SELECT usage_month, avg_msu
FROM CAPPLAN.MONTHLY_APP_USAGE
WHERE app_key = ?
  AND usage_month >= CURRENT DATE - 36 MONTHS
ORDER BY usage_month
