"""BLS macro data ingest.

Downloads OEWS (wages) and Employment Projections (10-year growth) bulk XLSX
files from BLS, parses them, filters to knowledge-work occupations, and stores
in DuckDB.

No API key required — these are public bulk downloads.
"""

import subprocess
import zipfile
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd
import yaml

# --- Config ---

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DB_PATH = PROJECT_ROOT / "data" / "jobs.duckdb"
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

# BLS bulk download URLs (no API key needed)
OEWS_NAT_URL = "https://www.bls.gov/oes/special-requests/oesm24nat.zip"
EMP_PROJECTIONS_URL = "https://www.bls.gov/emp/ind-occ-matrix/occupation.xlsx"

# BLS blocks requests without a Referer from their domain
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.bls.gov/oes/tables.htm",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Knowledge-work SOC prefixes
KNOWLEDGE_WORK_PREFIXES = ("11", "13", "15", "17", "19", "21", "23", "25", "27", "29")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def get_db_connection() -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH))


def _curl_download(url: str, dest: Path) -> Path:
    """Download using curl — BLS blocks Python requests via TLS fingerprinting."""
    # BLS requires: Referer from their domain + Accept-Encoding header
    if "oes/" in url:
        referer = "https://www.bls.gov/oes/tables.htm"
    elif "emp/" in url:
        referer = "https://www.bls.gov/emp/tables/occupational-projections-and-characteristics.htm"
    else:
        referer = "https://www.bls.gov/"
    cmd = [
        "curl",
        "-L",
        "-s",
        "-o",
        str(dest),
        "-H",
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "-H",
        "Accept-Language: en-US,en;q=0.5",
        "-H",
        "Accept-Encoding: gzip, deflate, br",
        "-H",
        f"Referer: {referer}",
        "-H",
        "Connection: keep-alive",
        "-H",
        "Upgrade-Insecure-Requests: 1",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr}")
    if not dest.exists() or dest.stat().st_size < 100:
        raise RuntimeError(f"Download failed — file too small or missing: {dest}")
    print(f"  [download] Saved {dest.stat().st_size / 1024:.0f} KB → {dest.name}")
    return dest


def download_file(url: str, dest: Path) -> Path:
    """Download a file using curl (BLS blocks Python requests)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if dest.stat().st_size < 5000:
            print(
                f"  [cache] Cached file too small ({dest.stat().st_size} bytes) — likely 403 error page, re-downloading..."
            )
            dest.unlink()
        else:
            print(f"  [cache] Using cached: {dest.name}")
            return dest
    print(f"  [download] {url}")
    return _curl_download(url, dest)


def download_and_extract_zip(url: str, dest_dir: Path) -> Path:
    """Download a zip, extract, return path to the first XLSX file."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / "oews_nat.zip"

    if not zip_path.exists():
        print(f"  [download] {url}")
        _curl_download(url, zip_path)
    elif zip_path.stat().st_size < 5000:
        print(
            f"  [cache] Cached file too small ({zip_path.stat().st_size} bytes) — likely 403 error page, re-downloading..."
        )
        zip_path.unlink()
        print(f"  [download] {url}")
        _curl_download(url, zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        xlsx_names = [n for n in zf.namelist() if n.endswith(".xlsx")]
        if not xlsx_names:
            raise ValueError(f"No XLSX found in {zip_path}")
        xlsx_path = dest_dir / xlsx_names[0]
        if not xlsx_path.exists():
            zf.extract(xlsx_names[0], dest_dir)
        return xlsx_path


# --- OEWS ingest ---


def ingest_oews(con: duckdb.DuckDBPyConnection) -> int:
    """Download and ingest OEWS national wage data.

    Returns number of rows inserted.
    """
    print("[OEWS] Downloading national wage data...")
    xlsx_path = download_and_extract_zip(OEWS_NAT_URL, CACHE_DIR / "oews")

    print(f"[OEWS] Reading {xlsx_path.name}...")
    # Get sheet name dynamically
    import openpyxl

    wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    sheet_name = wb.sheetnames[0]
    wb.close()
    df = pd.read_excel(xlsx_path, sheet_name=sheet_name)

    # OEWS columns: AREA, ST, STATE, OCC_CODE, OCC_TITLE, OCC_GROUP, TOT_EMP,
    # EMP_PRSE, JOBS_1000, LOC QUOTIENT, PCT_TOTAL, H_MEAN, MEAN_PRSE,
    # A_MEAN, MEAN_PRSE, H_MEDIAN, A_MEDIAN, ANNUAL, HOURLY, etc.
    # We care about: OCC_CODE, OCC_TITLE, OCC_GROUP, TOT_EMP, A_MEAN, A_MEDIAN

    # Standardize column names
    df.columns = df.columns.str.strip().str.upper()

    # Keep only line-item occupations (OCC_GROUP == 'detailed')
    # Some files use 'detailed', some use 'line item'
    if "OCC_GROUP" in df.columns:
        df = df[df["OCC_GROUP"].str.lower() == "detailed"]

    # Filter to knowledge-work SOC codes
    df = df[df["OCC_CODE"].astype(str).str[:2].isin(KNOWLEDGE_WORK_PREFIXES)]
    print(f"[OEWS] {len(df)} knowledge-work occupations after filter")

    # Select and rename columns
    result = pd.DataFrame(
        {
            "occupation_code": df["OCC_CODE"].astype(str),
            "occupation_title": df["OCC_TITLE"].astype(str),
            "total_employment": pd.to_numeric(df.get("TOT_EMP"), errors="coerce"),
            "annual_mean_wage": pd.to_numeric(df.get("A_MEAN"), errors="coerce"),
            "annual_median_wage": pd.to_numeric(df.get("A_MEDIAN"), errors="coerce"),
            "source": "oews_2024",
            "period": "May 2024",
        }
    )

    # Create table and insert
    con.execute("""
        CREATE TABLE IF NOT EXISTS bls_oews (
            occupation_code VARCHAR,
            occupation_title VARCHAR,
            total_employment DOUBLE,
            annual_mean_wage DOUBLE,
            annual_median_wage DOUBLE,
            source VARCHAR,
            period VARCHAR
        )
    """)

    # Clear existing data for this source/period
    con.execute("DELETE FROM bls_oews WHERE source = 'oews_2024'")

    con.register("oews_df", result)
    con.execute("INSERT INTO bls_oews SELECT * FROM oews_df")
    con.unregister("oews_df")

    print(f"[OEWS] Inserted {len(result)} rows into bls_oews")
    return len(result)


# --- Employment Projections ingest ---


def ingest_emp_projections(con: duckdb.DuckDBPyConnection) -> int:
    """Download and ingest Employment Projections 2024-2034 data.

    Returns number of rows inserted.
    """
    print("[EMP] Downloading employment projections...")
    xlsx_path = download_file(EMP_PROJECTIONS_URL, CACHE_DIR / "emp" / "occupation.xlsx")

    print(f"[EMP] Reading {xlsx_path.name}...")
    # The XLSX has multiple sheets — Table 1.2 is the occupational projections data
    # Row 0 = actual headers, row 1+ = data. The first row in the sheet is a title.
    df = pd.read_excel(xlsx_path, sheet_name="Table 1.2", header=0, skiprows=[1])

    # Now row 0 has the real column names
    # Re-read with proper header
    df = pd.read_excel(xlsx_path, sheet_name="Table 1.2")
    # First row contains the real headers
    df.columns = df.iloc[0]
    df = df.iloc[1:].reset_index(drop=True)

    # Standardize column names
    df.columns = df.columns.str.strip()

    # The columns are long names like:
    # "2024 National Employment Matrix title", "2024 National Employment Matrix code",
    # "Employment, 2024", "Employment, 2034", "Employment change, percent, 2024-34",
    # "Median annual wage, dollars, 2024", etc.

    # Find columns by substring match
    def find_col(df: pd.DataFrame, *substrings: str) -> Optional[str]:
        for col in df.columns:
            col_lower = str(col).lower()
            if all(s in col_lower for s in substrings):
                return col
        return None

    code_col = find_col(df, "matrix", "code") or find_col(df, "code")
    title_col = find_col(df, "matrix", "title") or find_col(df, "title")
    occ_type_col = find_col(df, "occupation", "type")
    emp_2024_col = find_col(df, "employment", "2024")
    emp_2034_col = find_col(df, "employment", "2034")
    change_pct_col = find_col(df, "change", "percent")
    openings_col = find_col(df, "openings")
    median_wage_col = find_col(df, "median", "wage")

    print("  [EMP] Columns mapped:")
    print(f"    code: {code_col}")
    print(f"    title: {title_col}")
    print(f"    emp_2024: {emp_2024_col}")
    print(f"    emp_2034: {emp_2034_col}")
    print(f"    change_pct: {change_pct_col}")
    print(f"    openings: {openings_col}")
    print(f"    median_wage: {median_wage_col}")

    if not all([code_col, title_col, emp_2024_col, emp_2034_col]):
        raise ValueError("Could not find required columns in employment projections data")

    # Filter to line items only
    if occ_type_col:
        df = df[df[occ_type_col].astype(str).str.lower() == "line item"]

    # The matrix code is like "11-1011" (SOC format)
    # Filter to knowledge-work
    df = df[df[code_col].astype(str).str[:2].isin(KNOWLEDGE_WORK_PREFIXES)]
    print(f"[EMP] {len(df)} knowledge-work occupations after filter")

    result = pd.DataFrame(
        {
            "occupation_code": df[code_col].astype(str),
            "occupation_title": df[title_col].astype(str),
            "employment_2024": pd.to_numeric(df[emp_2024_col], errors="coerce"),
            "employment_2034": pd.to_numeric(df[emp_2034_col], errors="coerce"),
            "employment_change_pct": pd.to_numeric(df[change_pct_col], errors="coerce")
            if change_pct_col
            else None,
            "annual_openings": pd.to_numeric(df[openings_col], errors="coerce")
            if openings_col
            else None,
            "median_annual_wage": pd.to_numeric(df[median_wage_col], errors="coerce")
            if median_wage_col
            else None,
            "source": "emp_projections_2024_2034",
        }
    )

    # Create table and insert
    con.execute("""
        CREATE TABLE IF NOT EXISTS bls_emp_projections (
            occupation_code VARCHAR,
            occupation_title VARCHAR,
            employment_2024 DOUBLE,
            employment_2034 DOUBLE,
            employment_change_pct DOUBLE,
            annual_openings DOUBLE,
            median_annual_wage DOUBLE,
            source VARCHAR
        )
    """)

    con.execute("DELETE FROM bls_emp_projections WHERE source = 'emp_projections_2024_2034'")
    con.register("emp_df", result)
    con.execute("INSERT INTO bls_emp_projections SELECT * FROM emp_df")
    con.unregister("emp_df")

    print(f"[EMP] Inserted {len(result)} rows into bls_emp_projections")
    return len(result)


# --- Main ---


def main():
    print("=" * 60)
    print("BLS Macro Ingest")
    print("=" * 60)

    con = get_db_connection()

    # OEWS (wages + employment)
    oews_count = ingest_oews(con)

    # Employment Projections (10-year growth)
    emp_count = ingest_emp_projections(con)

    # Summary
    print("\n" + "=" * 60)
    print("INGEST COMPLETE")
    print("=" * 60)
    print(f"  OEWS occupations:        {oews_count}")
    print(f"  Emp Projections:         {emp_count}")

    # Show top 10 by median wage from OEWS
    print("\nTop 10 knowledge-work occupations by median wage (OEWS 2024):")
    top_wages = con.execute("""
        SELECT occupation_code, occupation_title, annual_median_wage, total_employment
        FROM bls_oews
        WHERE annual_median_wage IS NOT NULL
        ORDER BY annual_median_wage DESC
        LIMIT 10
    """).fetchdf()
    print(top_wages.to_string(index=False))

    # Show top 10 by projected growth
    print("\nTop 10 knowledge-work occupations by projected growth % (2024-2034):")
    top_growth = con.execute("""
        SELECT occupation_code, occupation_title,
               employment_change_pct, employment_2024, employment_2034,
               median_annual_wage
        FROM bls_emp_projections
        WHERE employment_change_pct IS NOT NULL
        ORDER BY employment_change_pct DESC
        LIMIT 10
    """).fetchdf()
    print(top_growth.to_string(index=False))

    con.close()


if __name__ == "__main__":
    main()
