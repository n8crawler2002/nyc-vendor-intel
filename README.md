# NYC Street Vendor Intelligence

**Data quality, anomaly detection & operational analytics for 23,000 street vendors.**

## What this is

A data engineering exercise using real NYC Open Data to demonstrate hands-on competence with messy, multi-source public datasets. The context is CartZero — a clean energy battery swap service for NYC food cart vendors that replaces gasoline generators.

This is not a developer portfolio. It's proof that an operational leader can ingest, clean, query, and visualize data without handing it off to someone else.

## Data sources

| Source | Format | Records | Origin |
|--------|--------|---------|--------|
| DOHMH Mobile Food Vending Violations | CSV | ~86,000 | [NYC Open Data](https://data.cityofnewyork.us/Health/Mobile-Food-Vending/jz4z-kudi) |
| DPR Eateries (Parks concessions) | JSON | ~200 | [NYC Open Data](https://data.cityofnewyork.us/Recreation/DPR-Eateries/5bq2-fqcq) |
| Commissary database | CSV | 69 | DOHMH / manual compilation |
| OneNYC 2050 Strategic Plan | CSV | ~1,000 | [NYC Open Data](https://data.cityofnewyork.us/City-Government/OneNYC-2050/i3ck-6r6m) |

## What happens when you run it

```bash
pip install pandas folium geopy requests
python clean_and_analyze.py
```

The script runs five phases:

1. **Ingest** — Downloads datasets from NYC Open Data (or loads local copies). Prints schema summaries, null counts, column types.

2. **Clean** — Normalizes names, parses mixed-format dollar amounts (handles German vs. US decimal ambiguity), deduplicates tickets, filters MFV-related violations by keyword matching across multiple charge code columns. Every edge case is documented in the code.

3. **Anomaly detection** — Flags four patterns: vendors with high violations but zero payment (data gap or chronic non-compliance), same name appearing in multiple boroughs (multi-cart operator or name collision), enforcement blitz clusters (3+ violations within 7 days), and penalty-vs-paid discrepancies.

4. **SQL** — Loads cleaned data into SQLite, runs seven analytical queries: top vendors by fines, borough-year trends, tier distribution, commissary density, hub-vendor ratios, monthly seasonality, and top charge codes.

5. **Map** — Generates an interactive Folium map with commissary markers (color-coded by borough) and a vendor density heatmap. Geocodes addresses where possible, falls back to borough centroids with jitter for incomplete addresses.

## Outputs

```
output/
  map.html          — Interactive Folium map (open in browser)
  anomalies.csv     — Flagged data anomalies for review

data/clean/
  nyc_vendors.db    — SQLite database (violations, commissaries, parks_eateries)
  violations_clean.csv
  parks_clean.csv
  commissaries_clean.csv
```

## Edge cases documented in code

- **Decimal ambiguity:** `1.000,00` (German) vs. `1,000.00` (US) in financial columns — detected by comma/dot pattern and handled explicitly
- **Name normalization:** 12% of records have empty first or last names; names under 3 characters are flagged but retained for manual review
- **Unparseable dates:** Handled via `errors='coerce'` — NaT values counted and reported
- **Duplicate tickets:** Exact duplicates removed, keeping first occurrence
- **Incomplete addresses:** Geocoding falls back to borough centroid with random jitter to prevent marker stacking
- **Apple Numbers format:** The commissary database was originally in `.numbers` format (protobuf-wrapped IWA files inside a ZIP) — extracted via string parsing in a separate preprocessing step

## Data quality observations

Findings from the anomaly detection that illustrate real-world messiness in municipal datasets:

- **`NKNOWN` vendor (128 violations, $11,246 paid):** A truncated placeholder for `UNKNOWN` — violations where the officer did not record the vendor's name. Not a data error to fix, but a process artifact to exclude from vendor-level analysis. Tells you something about DOHMH field operations, not about a person.

- **71 vendors with high violation counts but $0 paid:** Either a payment recording lag, dismissed cases, or chronic non-compliance. Operationally: these are not first-contact targets for a sales conversation.

- **912 multi-borough vendors:** Same name appearing in more than one borough. Could be a multi-cart operator (legitimate) or a name collision (two different people named Mohammed Rahman). Requires field verification — data alone cannot disambiguate.

- **1,009 enforcement blitz patterns:** Vendors receiving 3+ violations within 7 days. This is a targeted sweep, not typical vendor behavior. Skews violation counts if used as a proxy for operator size.

- **12,770 records where paid < 10% of penalty:** Suggests widespread use of payment plans, adjudication reductions, or data entry timing gaps between penalty assessment and payment recording.

## Tech stack

Python 3.10+ · pandas · SQLite · Folium · geopy · requests

## Author

**Bodo Becker** — [be-eclectic.com](https://be-eclectic.com) · Climate Reality Leader · German Solar Prize 2019
