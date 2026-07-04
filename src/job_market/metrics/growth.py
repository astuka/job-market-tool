"""growth_score computation.

Composite score = weighted sum of:
  1. posting_count_yoy (30%) — YoY change in posting volume (from postings table)
  2. median_pay_yoy (30%) — YoY change in median advertised salary
  3. projected_10yr_growth (25%) — BLS 10-year employment projection % change
  4. inverse_hhi (15%) — 1 - Herfindahl index of employer concentration

Each component is normalized to [0, 1] before weighting.
"""

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DB_PATH = PROJECT_ROOT / "data" / "jobs.duckdb"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def get_db_connection() -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH))


def normalize(series: pd.Series) -> pd.Series:
    """Normalize a pandas Series to [0, 1] using min-max scaling.

    Returns 0.5 for all-NaN series (neutral score).
    """
    if series.isna().all() or series.nunique() == 0:
        return pd.Series(0.5, index=series.index)
    min_val = series.min()
    max_val = series.max()
    if min_val == max_val:
        return pd.Series(0.5, index=series.index)
    return (series - min_val) / (max_val - min_val)


def compute_hhi(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Compute Herfindahl-Hirschman Index per occupation from postings.

    HHI = sum of (employer_share)^2 for each occupation.
    Higher HHI = more concentrated (fewer employers).
    We return inverse_hhi = 1 - HHI so higher = better (more diverse).

    Since postings don't have SOC codes yet (will come with Adzuna in Phase 5),
    we group by employer across all postings as a proxy.
    """
    try:
        result = con.execute("""
            SELECT
                employer,
                COUNT(*) as posting_count
            FROM postings
            WHERE employer IS NOT NULL AND employer != ''
            GROUP BY employer
            ORDER BY posting_count DESC
        """).fetchdf()
    except Exception:
        return pd.DataFrame(columns=["employer", "posting_count"])

    if result.empty:
        return result

    total = result["posting_count"].sum()
    result["share"] = result["posting_count"] / total
    result["share_sq"] = result["share"] ** 2

    # Overall HHI (single value across all employers)
    hhi = result["share_sq"].sum()
    return result, hhi


def compute_growth_score() -> int:
    """Compute the composite growth_score for all occupations.

    Returns number of occupations scored.
    """
    config = load_config()
    weights = config.get("weights", {})
    w_posting = weights.get("posting_count_yoy", 30) / 100
    w_pay = weights.get("median_pay_yoy", 30) / 100
    w_proj = weights.get("projected_10yr_growth", 25) / 100
    w_hhi = weights.get("inverse_hhi", 15) / 100

    print("=" * 60)
    print("growth_score computation")
    print(
        f"  weights: posting_yoy={w_posting}, pay_yoy={w_pay}, "
        f"proj_10yr={w_proj}, inverse_hhi={w_hhi}"
    )
    print("=" * 60)

    con = get_db_connection()

    # --- Component 3: BLS 10-year projected growth ---
    print("\n[1/4] Fetching BLS 10-year projected growth...")
    try:
        bls_proj = con.execute("""
            SELECT occupation_code, occupation_title,
                   employment_change_pct, median_annual_wage,
                   annual_openings
            FROM bls_emp_projections
            WHERE employment_change_pct IS NOT NULL
        """).fetchdf()
        print(f"  {len(bls_proj)} occupations with BLS projections")
    except Exception as e:
        print(f"  No BLS projections table: {e}")
        bls_proj = pd.DataFrame()

    if bls_proj.empty:
        print("  No BLS data — cannot compute growth_score without macro data.")
        print("  Run `python -m job_market.ingest.bls` first.")
        con.close()
        return 0

    # --- Component 1: Posting count YoY ---
    print("\n[2/4] Computing posting count YoY...")
    try:
        posting_counts = con.execute("""
            SELECT
                employer,
                COUNT(*) as count,
                MIN(fetched_date) as earliest_fetch,
                MAX(fetched_date) as latest_fetch
            FROM postings
            GROUP BY employer
        """).fetchdf()
        print(f"  {len(posting_counts)} employers with postings")
    except Exception:
        print("  No postings table yet — posting_count_yoy will be neutral (0.5)")
        posting_counts = pd.DataFrame()

    # --- Component 2: Median pay YoY ---
    print("\n[3/4] Computing median pay from postings...")
    try:
        median_pay = con.execute("""
            SELECT
                AVG((salary_min + salary_max) / 2) as avg_salary
            FROM postings
            WHERE salary_min IS NOT NULL AND salary_max IS NOT NULL
        """).fetchone()
        overall_median = median_pay[0] if median_pay and median_pay[0] else 0
        print(f"  Overall avg salary across postings: ${overall_median:,.0f}")
    except Exception:
        print("  No salary data in postings — median_pay_yoy will use BLS wages only")
        overall_median = 0

    # --- Component 4: Inverse HHI (employer concentration) ---
    print("\n[4/4] Computing employer concentration (HHI)...")
    hhi_result = compute_hhi(con)
    if isinstance(hhi_result, tuple):
        employer_df, overall_hhi = hhi_result
        inverse_hhi = 1 - overall_hhi if overall_hhi else 0.5
        print(f"  Overall HHI: {overall_hhi:.4f}, inverse_hhi: {inverse_hhi:.4f}")
    else:
        inverse_hhi = 0.5
        print("  No postings for HHI — using neutral 0.5")

    # --- Build composite score ---
    print("\nBuilding composite growth_score...")

    # Start with BLS projections as the base
    result = bls_proj.copy()

    # Normalize BLS projected growth % to [0,1]
    result["proj_growth_norm"] = normalize(result["employment_change_pct"])

    # Median wage from BLS projections as pay component
    result["pay_norm"] = normalize(result["median_annual_wage"])

    # Posting count YoY — we don't have historical data yet (Phase 5 Adzuna will add this)
    # For now, use annual_openings as a proxy for posting volume
    result["posting_norm"] = normalize(result["annual_openings"])

    # Inverse HHI — same for all occupations since we don't have per-occupation postings yet
    result["hhi_norm"] = inverse_hhi

    # Compute composite score
    result["growth_score"] = (
        w_posting * result["posting_norm"]
        + w_pay * result["pay_norm"]
        + w_proj * result["proj_growth_norm"]
        + w_hhi * result["hhi_norm"]
    )

    # Round to 4 decimal places
    result["growth_score"] = result["growth_score"].round(4)

    # Select final columns
    final = result[
        [
            "occupation_code",
            "occupation_title",
            "growth_score",
            "employment_change_pct",
            "median_annual_wage",
            "annual_openings",
        ]
    ].copy()

    final.columns = [
        "soc_code",
        "occupation_title",
        "growth_score",
        "projected_10yr_growth_pct",
        "median_annual_wage",
        "annual_openings",
    ]

    # Add component scores
    final["component_posting"] = result["posting_norm"].round(4)
    final["component_pay"] = result["pay_norm"].round(4)
    final["component_proj"] = result["proj_growth_norm"].round(4)
    final["component_hhi"] = (
        result["hhi_norm"] if isinstance(result["hhi_norm"], (int, float)) else 0.5
    )
    final["computed_at"] = date.today()

    # Create/update growth_metrics table
    con.execute("""
        CREATE TABLE IF NOT EXISTS growth_metrics (
            soc_code VARCHAR,
            occupation_title VARCHAR,
            growth_score DOUBLE,
            projected_10yr_growth_pct DOUBLE,
            median_annual_wage DOUBLE,
            annual_openings DOUBLE,
            component_posting DOUBLE,
            component_pay DOUBLE,
            component_proj DOUBLE,
            component_hhi DOUBLE,
            computed_at DATE
        )
    """)

    # Clear and reload
    con.execute("DELETE FROM growth_metrics")
    con.register("metrics_df", final)
    con.execute("INSERT INTO growth_metrics SELECT * FROM metrics_df")
    con.unregister("metrics_df")

    print(f"\nComputed growth_score for {len(final)} occupations")

    # Display top 20
    print("\nTop 20 occupations by growth_score:")
    top20 = con.execute("""
        SELECT soc_code, occupation_title, growth_score,
               projected_10yr_growth_pct, median_annual_wage
        FROM growth_metrics
        ORDER BY growth_score DESC
        LIMIT 20
    """).fetchdf()
    print(top20.to_string(index=False))

    con.close()
    return len(final)


if __name__ == "__main__":
    compute_growth_score()
