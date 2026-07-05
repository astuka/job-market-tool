"""Adzuna job posting ingest.

Uses the Adzuna API to fetch:
- /jobs/search — live postings with salary data
- /jobs/history — historical monthly posting volume + average salary (for YoY growth)
- /jobs/histogram — salary distribution per occupation
- /jobs/top_companies — employer concentration (for HHI calculation)

API docs: https://developer.adzuna.com/docs/
"""

import os
import time
from datetime import date, datetime
from pathlib import Path

import requests
import yaml

from .db import ensure_postings_table, get_db_connection, insert_postings

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# Load .env file if it exists (for local dev)
ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs"

# Knowledge-work category tags on Adzuna (US market)
# Verified valid categories that return HTTP 200
KNOWLEDGE_WORK_CATEGORIES = [
    "it-jobs",
    "accounting-finance-jobs",
    "engineering-jobs",
    "healthcare-nursing-jobs",
    "legal-jobs",
    "consultancy-jobs",
    "hr-jobs",
    "creative-design-jobs",
]

# SOC code prefixes for knowledge work
KNOWLEDGE_WORK_PREFIXES = ("11", "13", "15", "17", "19", "21", "23", "25", "27", "29")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def get_credentials() -> tuple:
    """Get Adzuna API credentials from env vars or .env file."""
    app_id = os.environ.get("ADZUNA_APP_ID", "")
    app_key = os.environ.get("ADZUNA_APP_KEY", "")
    country = "us"

    if not app_id or not app_key:
        print("[Adzuna] No credentials found. Set ADZUNA_APP_ID and ADZUNA_APP_KEY env vars.")
        print("[Adzuna] Or create a .env file (see .env.example)")
        return "", "", country

    return app_id, app_key, country


def adzuna_request(endpoint: str, params: dict, max_retries: int = 5) -> dict:
    """Make an Adzuna API request with retry logic."""
    app_id, app_key, country = get_credentials()

    base_params = {
        "app_id": app_id,
        "app_key": app_key,
        "content-type": "application/json",
    }
    base_params.update(params)

    url = f"{ADZUNA_BASE}/{country}/{endpoint}"

    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=base_params, timeout=30)
            if response.status_code == 200:
                return response.json()
            elif response.status_code in (429, 503):
                wait = 3 * (attempt + 1)
                print(f"  [Adzuna] Rate limited (HTTP {response.status_code}), waiting {wait}s...")
                time.sleep(wait)
                continue
            else:
                print(f"  [Adzuna] HTTP {response.status_code}: {response.text[:200]}")
                if attempt < max_retries - 1:
                    time.sleep(3)
        except requests.RequestException as e:
            print(f"  [Adzuna] Request error: {e}")
            if attempt < max_retries - 1:
                time.sleep(3)

    return {}


def ingest_adzuna_search(max_pages: int = 10) -> int:
    """Fetch live postings from Adzuna /jobs/search.

    Filters to salary-disclosed + knowledge-work categories.
    Returns number of new postings inserted.
    """
    print("[Adzuna] Fetching live postings (search)...")

    all_rows = []
    total_count = 0

    for category in KNOWLEDGE_WORK_CATEGORIES:
        for page in range(1, max_pages + 1):
            params = {
                "results_per_page": 50,
                "category": category,
                "sort_by": "date",
                "salary_min": 1,  # Only postings with salary
                "where": "United States",
            }

            data = adzuna_request(f"search/{page}", params)
            results = data.get("results", [])

            if not results:
                break

            count = data.get("count", 0)
            if page == 1:
                print(f"  [Adzuna] Category '{category}': {count} total results")

            for job in results:
                salary_min = job.get("salary_min", 0) or 0
                salary_max = job.get("salary_max", 0) or 0

                # Must have salary attached
                if salary_min == 0 and salary_max == 0:
                    continue

                # Check if salary is predicted (not disclosed by employer)
                if job.get("salary_is_predicted") == "1":
                    continue

                title = job.get("title", "")
                company = (
                    job.get("company", {}).get("display_name", "")
                    if isinstance(job.get("company"), dict)
                    else ""
                )
                location_obj = job.get("location", {})
                location = (
                    location_obj.get("display_name", "") if isinstance(location_obj, dict) else ""
                )

                created_str = job.get("created", "")
                posted_date = None
                if created_str:
                    try:
                        posted_date = datetime.fromisoformat(created_str.replace("Z", "")).date()
                    except ValueError:
                        pass

                description = job.get("description", "")
                redirect_url = job.get("redirect_url", "")

                all_rows.append(
                    {
                        "source": "adzuna",
                        "employer": company,
                        "title": title,
                        "location": location,
                        "remote": False,
                        "salary_min": float(salary_min) if salary_min else None,
                        "salary_max": float(salary_max) if salary_max else None,
                        "salary_currency": "USD",
                        "description": description[:5000] if description else "",
                        "url": redirect_url,
                        "apply_url": redirect_url,
                        "soc_code": None,
                        "posted_date": posted_date,
                        "fetched_date": date.today(),
                        "salary_disclosed": True,
                    }
                )
                total_count += 1

            # Be polite — Adzuna rate limits aggressively
            time.sleep(1.5)

            # Check if we've exhausted results
            if len(results) < 50:
                break

        # Delay between categories
        time.sleep(2)

    print(f"  [Adzuna] {total_count} postings fetched (salary-disclosed + knowledge-work)")

    con = get_db_connection()
    ensure_postings_table(con)
    inserted, skipped = insert_postings(con, all_rows)
    con.close()

    print(f"  [Adzuna] Inserted {inserted}, skipped {skipped} duplicates")
    return inserted


def ingest_adzuna_history() -> dict:
    """Fetch historical posting volume + salary data from /jobs/history.

    Returns dict mapping category -> {month: avg_salary}.
    """
    print("[Adzuna] Fetching historical data (history)...")

    con = get_db_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS adzuna_history (
            category VARCHAR,
            month VARCHAR,
            avg_salary DOUBLE,
            fetched_date DATE
        )
    """)
    con.execute("DELETE FROM adzuna_history")

    all_data = {}

    for category in KNOWLEDGE_WORK_CATEGORIES:
        params = {
            "category": category,
            "location0": "US",
        }

        data = adzuna_request("history", params)
        month_data = data.get("month", {})

        if month_data:
            print(f"  [Adzuna] Category '{category}': {len(month_data)} months of data")
            all_data[category] = month_data

            for month, salary in month_data.items():
                con.execute(
                    "INSERT INTO adzuna_history VALUES (?, ?, ?, ?)",
                    [category, month, float(salary), date.today()],
                )

        time.sleep(1.5)  # Rate limit protection

    con.close()
    print(f"  [Adzuna] History data stored for {len(all_data)} categories")
    return all_data


def ingest_adzuna_histogram(category: str) -> dict:
    """Fetch salary distribution histogram for a category.

    Returns dict mapping salary_bucket -> vacancy_count.
    """
    params = {
        "category": category,
        "location0": "US",
    }

    data = adzuna_request("histogram", params)
    histogram = data.get("histogram", {})

    if histogram:
        print(f"  [Adzuna] Histogram for '{category}': {len(histogram)} salary buckets")

    return histogram


def ingest_adzuna_top_companies(category: str) -> list:
    """Fetch top companies hiring in a category (for HHI calculation).

    Returns list of {company, count}.
    """
    params = {
        "category": category,
        "location0": "US",
    }

    data = adzuna_request("top_companies", params)
    companies = data.get("top_companies", [])

    if companies:
        # companies is a list of {canonical_name: count} dicts
        result = []
        for item in companies:
            if isinstance(item, dict):
                for name, count in item.items():
                    result.append({"company": name, "count": count})
        print(f"  [Adzuna] Top companies for '{category}': {len(result)} companies")
        return result

    return []


def main():
    print("=" * 60)
    print("Adzuna Ingest")
    print("=" * 60)

    # 1. Search — live postings with salary
    search_count = ingest_adzuna_search(max_pages=5)

    # 2. History — historical posting volume + salary for YoY growth
    history_data = ingest_adzuna_history()

    # 3. Summary
    print("\n" + "=" * 60)
    print("ADZUNA INGEST COMPLETE")
    print("=" * 60)
    print(f"  Postings inserted:  {search_count}")
    print(f"  Categories tracked: {len(history_data)}")

    # Show salary trends for a few categories
    con = get_db_connection()
    print("\nSalary trends (last 6 months) for top categories:")
    trends = con.execute("""
        SELECT category, month, avg_salary
        FROM adzuna_history
        WHERE month >= strftime('%Y-%m', date('now', '-6 months'))
        ORDER BY category, month
        LIMIT 30
    """).fetchdf()

    if not trends.empty:
        for cat in trends["category"].unique():
            cat_data = trends[trends["category"] == cat].sort_values("month")
            if len(cat_data) >= 2:
                first = cat_data.iloc[0]
                last = cat_data.iloc[-1]
                yoy = ((last["avg_salary"] - first["avg_salary"]) / first["avg_salary"]) * 100
                print(
                    f"  {cat}: {first['month']} ${first['avg_salary']:,.0f} → "
                    f"{last['month']} ${last['avg_salary']:,.0f} ({yoy:+.1f}%)"
                )
    else:
        print("  No history data available yet")

    con.close()


if __name__ == "__main__":
    main()
