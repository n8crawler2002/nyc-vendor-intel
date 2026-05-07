#!/usr/bin/env python3
"""
NYC Street Vendor Intelligence — Data Quality & Anomaly Detection
=================================================================

Author:  Bodo Becker | be-eclectic.com
Context: CartZero — clean energy battery swap for NYC food cart vendors
Purpose: Demonstrate operational data competence:
         ingest, clean, detect anomalies, query, visualize.

Data sources (all public):
  1. DOHMH Mobile Food Vending Violations  — NYC Open Data  (CSV, ~86k rows)
  2. DPR Eateries in NYC Parks             — NYC Open Data  (JSON, ~200 records)
  3. Commissary Database                   — DOHMH / manual (CSV, 69 records)
  4. OneNYC 2050 Strategic Plan Indicators — NYC Open Data  (CSV, ~1k rows)

Run:
  pip install pandas folium geopy requests
  python clean_and_analyze.py

Outputs:
  data/          — cleaned CSVs + SQLite database
  output/        — Folium map (map.html), anomaly report (anomalies.csv)
"""

import os, sys, json, csv, sqlite3, warnings
from pathlib import Path
from datetime import datetime
from collections import Counter

import pandas as pd

# ── optional imports (degrade gracefully) ──────────────────────────
try:
    import folium
    from folium.plugins import HeatMap, MarkerCluster
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False
    print("⚠  folium not installed — map generation will be skipped.")

try:
    from geopy.geocoders import Nominatim
    from geopy.extra.rate_limiter import RateLimiter
    HAS_GEOPY = True
except ImportError:
    HAS_GEOPY = False
    print("⚠  geopy not installed — geocoding will use fallback coordinates.")

warnings.filterwarnings("ignore", category=FutureWarning)

# ── paths ──────────────────────────────────────────────────────────
ROOT    = Path(__file__).parent
RAW     = ROOT / "data" / "raw"
CLEAN   = ROOT / "data" / "clean"
OUTPUT  = ROOT / "output"
DB_PATH = CLEAN / "nyc_vendors.db"

for d in [RAW, CLEAN, OUTPUT]:
    d.mkdir(parents=True, exist_ok=True)

# ── download helpers ───────────────────────────────────────────────
NYC_OPEN_DATA = {
    "violations": {
        "url": "https://data.cityofnewyork.us/api/views/jz4z-kudi/rows.csv?accessType=DOWNLOAD",
        "file": "Mobile_Food_Vending.csv",
        "desc": "DOHMH Mobile Food Vending Violations (~86k rows)"
    },
    "dpr_eateries": {
        "url": "https://data.cityofnewyork.us/api/views/5bq2-fqcq/rows.json?accessType=DOWNLOAD",
        "file": "DPR_Eateries.json",
        "desc": "Parks & Recreation Eateries (JSON)"
    },
    "onenyc": {
        "url": "https://data.cityofnewyork.us/api/views/i3ck-6r6m/rows.csv?accessType=DOWNLOAD",
        "file": "OneNYC_2050.csv",
        "desc": "OneNYC 2050 Strategic Plan Indicators"
    },
}

def download_if_missing(key: str) -> Path:
    """Download a dataset from NYC Open Data if not already present locally."""
    info = NYC_OPEN_DATA[key]
    target = RAW / info["file"]
    if target.exists():
        print(f"  ✓ {info['desc']} — already present")
        return target
    try:
        import requests
        print(f"  ↓ Downloading {info['desc']}…")
        r = requests.get(info["url"], timeout=120)
        r.raise_for_status()
        target.write_bytes(r.content)
        print(f"    saved → {target.name} ({len(r.content)/1e6:.1f} MB)")
    except Exception as e:
        print(f"  ✗ Download failed: {e}")
        print(f"    → Place the file manually at: {target}")
        sys.exit(1)
    return target


# ═══════════════════════════════════════════════════════════════════
# PHASE 1 — INGEST & SCHEMA DISCOVERY
# ═══════════════════════════════════════════════════════════════════

def phase1_ingest() -> dict:
    """Load all raw data sources and print schema summaries."""
    print("\n" + "="*70)
    print("PHASE 1 — INGEST & SCHEMA DISCOVERY")
    print("="*70)

    datasets = {}

    # 1a) Violations CSV
    path = download_if_missing("violations")
    df = pd.read_csv(path, low_memory=False)
    datasets["violations"] = df
    print(f"\n[violations] {df.shape[0]:,} rows × {df.shape[1]} cols")
    print(f"  Nulls per column (top 5):")
    nulls = df.isnull().sum().sort_values(ascending=False).head(5)
    for col, n in nulls.items():
        print(f"    {col}: {n:,} ({n/len(df)*100:.1f}%)")

    # 1b) DPR Eateries JSON
    path = download_if_missing("dpr_eateries")
    raw_json = json.loads(path.read_text())
    # NYC Open Data JSON wraps rows inside "data" key with "meta" header
    if isinstance(raw_json, dict) and "data" in raw_json:
        cols = [c["fieldName"] for c in raw_json["meta"]["view"]["columns"]]
        parks_df = pd.DataFrame(raw_json["data"], columns=cols)
    elif isinstance(raw_json, list):
        parks_df = pd.DataFrame(raw_json)
    else:
        parks_df = pd.DataFrame()
    datasets["parks"] = parks_df
    print(f"\n[parks] {parks_df.shape[0]:,} rows × {parks_df.shape[1]} cols")

    # 1c) Commissary CSV (manually compiled — 69 records)
    comm_path = RAW / "commissaries.csv"
    if comm_path.exists():
        comm_df = pd.read_csv(comm_path)
        datasets["commissaries"] = comm_df
        print(f"\n[commissaries] {comm_df.shape[0]:,} rows × {comm_df.shape[1]} cols")
    else:
        print(f"\n[commissaries] ⚠ Not found at {comm_path}")
        print(f"  → Place your commissary CSV there (cols: Name,Address,Borough,Zip,Phone,Frozen)")
        datasets["commissaries"] = pd.DataFrame()

    # 1d) OneNYC 2050 CSV
    path = download_if_missing("onenyc")
    nyc50 = pd.read_csv(path, low_memory=False)
    datasets["onenyc"] = nyc50
    print(f"\n[onenyc] {nyc50.shape[0]:,} rows × {nyc50.shape[1]} cols")

    return datasets


# ═══════════════════════════════════════════════════════════════════
# PHASE 2 — DATA CLEANING (documented edge cases)
# ═══════════════════════════════════════════════════════════════════

def clean_amount(val) -> float:
    """
    Parse dollar amounts with mixed formatting.

    EDGE CASE: NYC Open Data exports sometimes contain German-style decimals
    (1.000,00) when opened in European-locale Excel and re-saved.
    Also handles: $1,234.56 | 1234 | empty | NaN.

    Decision logic:
      - If string has one comma AND dots: German format → remove dots, comma→dot
      - If string has one dot AND commas: US format → remove commas
      - If only commas (ambiguous): treat last comma as decimal separator
      - Fallback: return 0.0
    """
    if pd.isna(val):
        return 0.0
    s = str(val).replace("$", "").replace(" ", "").strip()
    if not s:
        return 0.0
    dots = s.count(".")
    commas = s.count(",")
    if commas == 1 and dots > 0:
        # German: 1.000,00 → 1000.00
        s = s.replace(".", "").replace(",", ".")
    elif dots == 1 and commas > 0:
        # US: 1,000.00 → 1000.00
        s = s.replace(",", "")
    elif commas > 0:
        # Ambiguous: treat last comma as decimal
        s = s.replace(",", ".", 1) if commas == 1 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def normalize_name(first, last) -> str:
    """
    Merge first + last name into a canonical form.

    EDGE CASE: ~12% of records have empty first OR last names.
    Some have leading/trailing whitespace or mixed case.
    Names shorter than 3 chars are likely data artifacts (initials only)
    and get flagged but not removed — that's a downstream decision.
    """
    f = str(first).strip() if pd.notna(first) else ""
    l = str(last).strip() if pd.notna(last) else ""
    return f"{f} {l}".strip().upper()


def phase2_clean(datasets: dict) -> dict:
    """Clean all datasets, document every decision."""
    print("\n" + "="*70)
    print("PHASE 2 — DATA CLEANING")
    print("="*70)

    # ── 2a) Violations ─────────────────────────────────────────────
    df = datasets["violations"].copy()
    n_raw = len(df)

    # Parse amounts
    for col in ["Paid Amount", "Penalty Imposed", "Total Violation Amount"]:
        if col in df.columns:
            df[f"{col}_clean"] = df[col].apply(clean_amount)

    # Normalize names
    df["FULL_NAME"] = df.apply(
        lambda r: normalize_name(r.get("Respondent First Name"), r.get("Respondent Last Name")),
        axis=1
    )

    # Flag short names but keep them — operational decision to review later
    short_names = df["FULL_NAME"].str.len() < 3
    print(f"\n[violations] Raw rows: {n_raw:,}")
    print(f"  Short names (<3 chars): {short_names.sum():,} — flagged, not removed")

    # Parse dates
    df["violation_date"] = pd.to_datetime(df.get("Violation Date"), errors="coerce")
    bad_dates = df["violation_date"].isna().sum()
    print(f"  Unparseable dates: {bad_dates:,}")

    # MFV keyword filter — narrow to mobile food vending related violations
    charge_cols = [c for c in df.columns if "Code Description" in c]
    MFV_KEYWORDS = ["VENDING", "COMMISSARY", "MOBILE", "CART", "VENDOR", "GENERATOR", "FUEL"]
    mfv_mask = pd.Series(False, index=df.index)
    for col in charge_cols:
        for kw in MFV_KEYWORDS:
            mfv_mask |= df[col].fillna("").str.upper().str.contains(kw, regex=False)
    df["is_mfv"] = mfv_mask
    print(f"  MFV-related rows: {mfv_mask.sum():,} / {n_raw:,} ({mfv_mask.sum()/n_raw*100:.1f}%)")

    # Remove duplicate ticket numbers (exact dupes only)
    if "Ticket Number" in df.columns:
        dupes = df.duplicated(subset=["Ticket Number"], keep="first").sum()
        df = df.drop_duplicates(subset=["Ticket Number"], keep="first")
        print(f"  Duplicate tickets removed: {dupes:,}")

    datasets["violations_clean"] = df

    # ── 2b) Parks eateries ─────────────────────────────────────────
    parks = datasets["parks"].copy()
    if not parks.empty:
        # Normalize date columns
        for dcol in ["start_date", "end_date"]:
            if dcol in parks.columns:
                parks[dcol] = pd.to_datetime(parks[dcol], errors="coerce")
        # Flag active permits (end_date in the future)
        if "end_date" in parks.columns:
            parks["is_active"] = parks["end_date"] >= pd.Timestamp.now()
            print(f"\n[parks] Active permits: {parks['is_active'].sum()} / {len(parks)}")
        datasets["parks_clean"] = parks

    # ── 2c) Commissaries ───────────────────────────────────────────
    comm = datasets.get("commissaries", pd.DataFrame()).copy()
    if not comm.empty:
        comm.columns = [c.strip().lower().replace(" ", "_") for c in comm.columns]
        # Standardize borough names
        borough_map = {
            "QN": "Queens", "QU": "Queens", "QUEENS": "Queens",
            "BX": "Bronx", "BRONX": "Bronx",
            "BK": "Brooklyn", "BROOKLYN": "Brooklyn",
            "MN": "Manhattan", "MANHATTAN": "Manhattan",
            "SI": "Staten Island", "STATEN ISLAND": "Staten Island",
        }
        if "borough" in comm.columns:
            comm["borough"] = comm["borough"].str.strip().str.upper().map(
                lambda x: borough_map.get(x, x)
            )
        print(f"\n[commissaries] {len(comm)} records cleaned")
        datasets["commissaries_clean"] = comm

    return datasets


# ═══════════════════════════════════════════════════════════════════
# PHASE 3 — ANOMALY DETECTION
# ═══════════════════════════════════════════════════════════════════

def phase3_anomalies(datasets: dict) -> pd.DataFrame:
    """Detect and log data anomalies for operational review."""
    print("\n" + "="*70)
    print("PHASE 3 — ANOMALY DETECTION")
    print("="*70)

    df = datasets["violations_clean"]
    mfv = df[df["is_mfv"]].copy()
    anomalies = []

    # ── 3a) High violations, zero payment ──────────────────────────
    # Interpretation: either a data gap or chronic non-compliance.
    # Operationally: these vendors may not be good first-contact targets.
    agg = mfv.groupby("FULL_NAME").agg(
        violations=("FULL_NAME", "count"),
        total_paid=("Paid Amount_clean", "sum"),
        latest=("violation_date", "max"),
    ).reset_index()

    ghosts = agg[(agg["violations"] >= 10) & (agg["total_paid"] == 0)]
    for _, row in ghosts.iterrows():
        anomalies.append({
            "type": "HIGH_VIOLATIONS_ZERO_PAID",
            "entity": row["FULL_NAME"],
            "detail": f"{row['violations']} violations, $0 paid — data gap or non-compliant",
            "severity": "review",
        })
    print(f"\n  High violations + $0 paid: {len(ghosts)} vendors")

    # ── 3b) Borough mismatch — same name, multiple boroughs ───────
    # Interpretation: either a multi-cart operator working across boroughs
    # (legitimate) or a name collision (two different people).
    multi_boro = mfv.groupby("FULL_NAME")["Violation Location (Borough)"].nunique()
    multi_boro = multi_boro[multi_boro > 1]
    for name, n_boro in multi_boro.items():
        if len(name) < 3:
            continue
        boroughs = mfv[mfv["FULL_NAME"] == name]["Violation Location (Borough)"].unique()
        anomalies.append({
            "type": "MULTI_BOROUGH",
            "entity": name,
            "detail": f"Appears in {n_boro} boroughs: {', '.join(str(b) for b in boroughs)}",
            "severity": "info",
        })
    print(f"  Multi-borough vendors: {len(multi_boro)}")

    # ── 3c) Enforcement blitz — 3+ violations in 7 days ───────────
    # Interpretation: a targeted enforcement sweep, not typical vendor
    # behavior. Skews violation counts if not accounted for.
    mfv_sorted = mfv.sort_values(["FULL_NAME", "violation_date"])
    blitz_count = 0
    for name, grp in mfv_sorted.groupby("FULL_NAME"):
        if len(grp) < 3:
            continue
        dates = grp["violation_date"].dropna().sort_values()
        for i in range(len(dates) - 2):
            window = dates.iloc[i:i+3]
            if (window.iloc[-1] - window.iloc[0]).days <= 7:
                blitz_count += 1
                anomalies.append({
                    "type": "ENFORCEMENT_BLITZ",
                    "entity": name,
                    "detail": f"3+ violations within 7 days around {window.iloc[0].strftime('%Y-%m-%d')}",
                    "severity": "context",
                })
                break  # one flag per vendor is enough
    print(f"  Enforcement blitz patterns: {blitz_count} vendors")

    # ── 3d) Penalty vs. paid discrepancy ───────────────────────────
    # When penalty_imposed >> paid_amount, there's either a payment plan,
    # a dismissal, or a data recording lag.
    if "Penalty Imposed_clean" in mfv.columns:
        mfv_pay = mfv[(mfv["Penalty Imposed_clean"] > 0)].copy()
        mfv_pay["pay_ratio"] = mfv_pay["Paid Amount_clean"] / mfv_pay["Penalty Imposed_clean"]
        underpaid = mfv_pay[mfv_pay["pay_ratio"] < 0.1]  # paid less than 10% of penalty
        print(f"  Penalty >> Paid (ratio < 10%): {len(underpaid):,} records")
        if len(underpaid) > 0:
            anomalies.append({
                "type": "PAYMENT_DISCREPANCY_SUMMARY",
                "entity": "AGGREGATE",
                "detail": f"{len(underpaid):,} records where paid < 10% of penalty imposed",
                "severity": "review",
            })

    # ── Save anomalies ─────────────────────────────────────────────
    anomaly_df = pd.DataFrame(anomalies)
    anomaly_path = OUTPUT / "anomalies.csv"
    anomaly_df.to_csv(anomaly_path, index=False)
    print(f"\n  → Saved {len(anomaly_df)} anomalies to {anomaly_path.name}")
    return anomaly_df


# ═══════════════════════════════════════════════════════════════════
# PHASE 4 — SQL LAYER (SQLite)
# ═══════════════════════════════════════════════════════════════════

def phase4_sql(datasets: dict):
    """Load cleaned data into SQLite and run analytical queries."""
    print("\n" + "="*70)
    print("PHASE 4 — SQL LAYER")
    print("="*70)

    conn = sqlite3.connect(str(DB_PATH))

    # ── Load violations ────────────────────────────────────────────
    df = datasets["violations_clean"]
    mfv = df[df["is_mfv"]].copy()

    # Select columns for SQL — keep what matters operationally
    # Include first charge code column for violation type analysis
    sql_cols = [
        "Ticket Number", "FULL_NAME", "Violation Location (Borough)",
        "violation_date", "Paid Amount_clean", "Penalty Imposed_clean",
        "is_mfv",
    ]
    # Dynamically add the first charge code column if it exists
    charge_cols = [c for c in mfv.columns if "Code Description" in c]
    if charge_cols:
        sql_cols.append(charge_cols[0])  # e.g. "Charge 1: Code Description"

    existing = [c for c in sql_cols if c in mfv.columns]
    mfv_slim = mfv[existing].copy()
    mfv_slim.columns = [
        c.lower().replace(" ", "_").replace("(", "").replace(")", "").replace(":", "").replace("#", "")
        for c in mfv_slim.columns
    ]
    # Print actual column names for debugging SQL queries
    print(f"\n  SQL column names: {list(mfv_slim.columns)}")
    mfv_slim.to_sql("violations", conn, if_exists="replace", index=False)
    print(f"  violations table: {len(mfv_slim):,} rows")

    # ── Load commissaries ──────────────────────────────────────────
    comm = datasets.get("commissaries_clean", pd.DataFrame())
    if not comm.empty:
        comm.to_sql("commissaries", conn, if_exists="replace", index=False)
        print(f"  commissaries table: {len(comm):,} rows")

    # ── Load parks eateries ────────────────────────────────────────
    parks = datasets.get("parks_clean", pd.DataFrame())
    if not parks.empty:
        parks.to_sql("parks_eateries", conn, if_exists="replace", index=False)
        print(f"  parks_eateries table: {len(parks):,} rows")

    # ── Run analytical queries ─────────────────────────────────────
    queries = load_queries()
    print(f"\n  Running {len(queries)} analytical queries…\n")

    for name, sql in queries.items():
        print(f"  ── {name} ──")
        try:
            result = pd.read_sql_query(sql, conn)
            print(result.to_string(index=False, max_rows=10))
        except Exception as e:
            print(f"  ✗ Error: {e}")
        print()

    conn.close()
    print(f"  → Database saved to {DB_PATH.name}")


def load_queries() -> dict:
    """Load SQL queries from queries.sql or use built-in defaults."""
    queries_file = ROOT / "queries.sql"
    if queries_file.exists():
        # Parse named queries separated by "-- @name: <query_name>"
        raw = queries_file.read_text()
        queries = {}
        current_name = None
        current_sql = []
        for line in raw.split("\n"):
            if line.strip().startswith("-- @name:"):
                if current_name and current_sql:
                    queries[current_name] = "\n".join(current_sql)
                current_name = line.split("-- @name:")[1].strip()
                current_sql = []
            else:
                current_sql.append(line)
        if current_name and current_sql:
            queries[current_name] = "\n".join(current_sql)
        return queries

    # Fallback: built-in queries
    return {
        "Top 10 vendors by cumulative fines": """
            SELECT full_name,
                   COUNT(*) AS violations,
                   violation_location_borough AS borough,
                   ROUND(SUM(paid_amount_clean), 2) AS total_paid,
                   MAX(violation_date) AS latest
            FROM violations
            WHERE LENGTH(full_name) >= 3
            GROUP BY full_name
            ORDER BY total_paid DESC
            LIMIT 10;
        """,
        "Violations per borough per year (trend)": """
            SELECT violation_location_borough AS borough,
                   SUBSTR(violation_date, 1, 4) AS year,
                   COUNT(*) AS violations
            FROM violations
            WHERE violation_date IS NOT NULL
              AND violation_location_borough IS NOT NULL
            GROUP BY borough, year
            ORDER BY borough, year;
        """,
        "Vendor tier distribution (active since 2020)": """
            SELECT CASE
                     WHEN cnt >= 40 THEN 'Platinum'
                     WHEN cnt >= 25 THEN 'Gold'
                     WHEN cnt >= 15 THEN 'Silver'
                     ELSE 'Bronze'
                   END AS tier,
                   COUNT(*) AS vendors,
                   ROUND(AVG(cnt), 1) AS avg_violations,
                   ROUND(SUM(total_paid), 2) AS sum_paid
            FROM (
                SELECT full_name,
                       COUNT(*) AS cnt,
                       SUM(paid_amount_clean) AS total_paid,
                       MAX(violation_date) AS latest
                FROM violations
                WHERE LENGTH(full_name) >= 3
                GROUP BY full_name
                HAVING latest >= '2020-01-01'
            )
            GROUP BY tier
            ORDER BY avg_violations DESC;
        """,
        "Commissaries per borough": """
            SELECT borough, COUNT(*) AS hubs,
                   SUM(CASE WHEN phone IS NOT NULL AND phone != '' THEN 1 ELSE 0 END) AS with_phone
            FROM commissaries
            GROUP BY borough
            ORDER BY hubs DESC;
        """,
        "Potential hub-vendor match by borough": """
            SELECT c.borough,
                   c.hubs AS commissaries,
                   v.vendors,
                   ROUND(CAST(v.vendors AS FLOAT) / c.hubs, 1) AS vendors_per_hub
            FROM (
                SELECT borough, COUNT(*) AS hubs FROM commissaries GROUP BY borough
            ) c
            LEFT JOIN (
                SELECT violation_location_borough AS borough,
                       COUNT(DISTINCT full_name) AS vendors
                FROM violations
                WHERE LENGTH(full_name) >= 3
                GROUP BY violation_location_borough
            ) v ON UPPER(c.borough) = UPPER(v.borough)
            ORDER BY vendors_per_hub DESC;
        """,
        "Monthly violation trend (seasonality check)": """
            SELECT SUBSTR(violation_date, 6, 2) AS month,
                   COUNT(*) AS violations,
                   ROUND(AVG(paid_amount_clean), 2) AS avg_paid
            FROM violations
            WHERE violation_date IS NOT NULL
            GROUP BY month
            ORDER BY month;
        """,
        "Top charge codes (what are vendors actually cited for?)": """
            SELECT charge_1_code_description AS charge,
                   COUNT(*) AS occurrences,
                   ROUND(AVG(paid_amount_clean), 2) AS avg_fine
            FROM violations
            WHERE charge_1_code_description IS NOT NULL
              AND charge_1_code_description != ''
            GROUP BY charge
            ORDER BY occurrences DESC
            LIMIT 15;
        """,
    }


# ═══════════════════════════════════════════════════════════════════
# PHASE 5 — FOLIUM MAP
# ═══════════════════════════════════════════════════════════════════

# Fallback coordinates for borough centers (no geocoding needed)
BOROUGH_COORDS = {
    "Manhattan":     (40.7831, -73.9712),
    "Brooklyn":      (40.6782, -73.9442),
    "Queens":        (40.7282, -73.7949),
    "Bronx":         (40.8448, -73.8648),
    "Staten Island": (40.5795, -74.1502),
}

BOROUGH_COLORS = {
    "Manhattan": "#e63946",
    "Brooklyn":  "#457b9d",
    "Queens":    "#2a9d8f",
    "Bronx":     "#e9c46a",
    "Staten Island": "#264653",
}


def geocode_address(address: str, borough: str, geocoder=None) -> tuple:
    """
    Geocode an address. Falls back to borough centroid if geocoding fails.

    EDGE CASE: Many commissary addresses are incomplete or have typos
    (e.g., 'DMADINA ST' instead of 'Medina St'). Geocoding will fail for
    these — the fallback ensures we still get them on the map at borough level.
    """
    if geocoder and address and len(address) > 5:
        try:
            query = f"{address}, {borough}, New York, NY"
            loc = geocoder(query)
            if loc:
                return (loc.latitude, loc.longitude)
        except Exception:
            pass
    # Fallback: borough centroid with small jitter to avoid overlap
    import random
    base = BOROUGH_COORDS.get(borough, (40.7128, -74.0060))
    jitter = (random.uniform(-0.008, 0.008), random.uniform(-0.008, 0.008))
    return (base[0] + jitter[0], base[1] + jitter[1])


def phase5_map(datasets: dict):
    """Generate an interactive Folium map of the vendor ecosystem."""
    print("\n" + "="*70)
    print("PHASE 5 — MAP VISUALIZATION")
    print("="*70)

    if not HAS_FOLIUM:
        print("  Skipping — install folium: pip install folium")
        return

    m = folium.Map(location=[40.7580, -73.9855], zoom_start=11,
                   tiles="CartoDB positron")

    # ── Commissary markers ─────────────────────────────────────────
    comm = datasets.get("commissaries_clean", pd.DataFrame())
    geocoder = None
    if HAS_GEOPY:
        geolocator = Nominatim(user_agent="nyc_vendor_intel")
        geocoder = RateLimiter(geolocator.geocode, min_delay_seconds=1.1)

    if not comm.empty:
        comm_group = folium.FeatureGroup(name="Commissaries (69 hubs)")
        geocoded_count = 0
        for _, row in comm.iterrows():
            borough = str(row.get("borough", ""))
            address = str(row.get("address", ""))
            name = str(row.get("name", "Unknown"))
            phone = str(row.get("phone", ""))

            lat, lon = geocode_address(address, borough, geocoder)
            color = BOROUGH_COLORS.get(borough, "#666")

            popup_html = f"<b>{name}</b><br>{address}<br>{borough}"
            if phone:
                popup_html += f"<br>📞 {phone}"

            folium.CircleMarker(
                location=[lat, lon],
                radius=7,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.7,
                popup=folium.Popup(popup_html, max_width=250),
                tooltip=name,
            ).add_to(comm_group)
            geocoded_count += 1

        comm_group.add_to(m)
        print(f"  Commissary markers: {geocoded_count}")

    # ── Vendor density heatmap by borough ──────────────────────────
    df = datasets["violations_clean"]
    mfv = df[df["is_mfv"]].copy()
    borough_counts = mfv["Violation Location (Borough)"].value_counts()

    heat_data = []
    for borough, count in borough_counts.items():
        if borough in BOROUGH_COORDS:
            lat, lon = BOROUGH_COORDS[borough]
            # Weight by relative vendor density
            heat_data.append([lat, lon, count / borough_counts.max()])

    if heat_data:
        HeatMap(
            heat_data,
            name="Vendor density (borough level)",
            radius=40,
            blur=25,
            min_opacity=0.3,
        ).add_to(m)
        print(f"  Heatmap layers: {len(heat_data)} boroughs")

    # ── Layer control ──────────────────────────────────────────────
    folium.LayerControl().add_to(m)

    # ── Legend ──────────────────────────────────────────────────────
    legend_html = """
    <div style="position:fixed; bottom:30px; left:30px; z-index:1000;
         background:white; padding:12px 16px; border-radius:8px;
         box-shadow:0 2px 8px rgba(0,0,0,0.2); font-family:sans-serif; font-size:12px;">
      <b>NYC Vendor Ecosystem</b><br>
      <span style="color:#e63946">●</span> Manhattan &nbsp;
      <span style="color:#457b9d">●</span> Brooklyn &nbsp;
      <span style="color:#2a9d8f">●</span> Queens<br>
      <span style="color:#e9c46a">●</span> Bronx &nbsp;
      <span style="color:#264653">●</span> Staten Island<br>
      <i>Circles = Commissary hubs | Heatmap = Vendor density</i>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    map_path = OUTPUT / "map.html"
    m.save(str(map_path))
    print(f"\n  → Map saved to {map_path.name}")


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    print("NYC Street Vendor Intelligence")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("Author: Bodo Becker | be-eclectic.com")

    datasets = phase1_ingest()
    datasets = phase2_clean(datasets)
    anomalies = phase3_anomalies(datasets)
    phase4_sql(datasets)
    phase5_map(datasets)

    # ── Save cleaned CSVs ──────────────────────────────────────────
    for key in ["violations_clean", "parks_clean", "commissaries_clean"]:
        if key in datasets and not datasets[key].empty:
            path = CLEAN / f"{key}.csv"
            datasets[key].to_csv(path, index=False)
            print(f"  → {path.name}")

    print("\n" + "="*70)
    print("DONE — all outputs in output/ and data/clean/")
    print("="*70)


if __name__ == "__main__":
    main()
