"""USAJobs.gov posting ingest.

Fetches from https://data.usajobs.gov/api/search — requires API key (free registration).
Federal pay scales are public data — salary is always disclosed in the form of GS grades.

NOTE: This source requires an API key from https://developer.usajobs.gov/apirequest/
Until Jacob registers and provides a key, this module will skip gracefully.
"""

import os
import time
from datetime import date, datetime

import requests

from .db import ensure_postings_table, get_db_connection, insert_postings

USAJOBS_URL = "https://data.usajobs.gov/api/search"

# Knowledge-work occupational series codes (federal equivalent of SOC codes)
# https://www.usajobs.gov/Help/working-in-government/unique-hiring-paths/
KNOWLEDGE_WORK_SERIES = [
    "2210",  # Information Technology Management
    "0301",  # Miscellaneous Administration and Program
    "0343",  # Management and Program Analysis
    "0344",  # Administrative Clerical and Assistance
    "0346",  # Marketing
    "0350",  # Editorial Services
    "0401",  # General Natural Resources Management and Biological Sciences
    "0405",  # Biological Science
    "0485",  # Fisheries Biology
    "0601",  # General Medical and Healthcare
    "0685",  # Nursing
    "0660",  # Medical Officer
    "0667",  # Health Scientist
    "1101",  # General Business and Industry
    "1102",  # Contracting
    "1103",  # Industrial Property Management
    "1165",  # Financial Institution Examining
    "1201",  # General Education and Training
    "1210",  # Accounting
    "1220",  # Auditing
    "1340",  # Meteorology
    "1520",  # Mathematics
    "1530",  # Statistics
    "1550",  # Computer Science
    "1701",  # General Education and Training
    "1710",  # Education Services
    "1801",  # General Inspection, Investigation, and Compliance
    "1905",  # Economic Analysis
    "1920",  # General Industry
    "1990",  # Economics
    "2199",  # Tax Administration
    "2210",  # IT
    "2300",  # Information and Arts
    "2334",  # Multimedia
    "2401",  # General Education and Training
]

# GS pay scale base (2024 GS-15 step 1 ~ $111,438, step 10 ~ $145,617)
# Convert GS grade to approximate salary range
GS_SALARY_TABLE = {
    # grade: (min, max) in USD
    1: (19513, 25461),
    2: (21937, 28654),
    3: (23872, 31179),
    4: (26774, 34975),
    5: (29920, 38952),
    6: (33515, 43650),
    7: (37353, 48643),
    8: (41446, 53985),
    9: (45832, 59706),
    10: (50513, 65807),
    11: (55529, 72235),
    12: (66689, 86686),
    13: (79307, 103136),
    14: (93737, 122005),
    15: (111438, 145617),
}


def gs_to_salary(gs_str: str) -> tuple:
    """Convert GS grade string (e.g. 'GS-13') to (min, max) salary."""
    if not gs_str:
        return None, None
    # Extract grade number
    import re

    match = re.search(r"gs[-\s]*(\d+)", gs_str.lower())
    if not match:
        return None, None
    grade = int(match.group(1))
    if grade in GS_SALARY_TABLE:
        return GS_SALARY_TABLE[grade][0], GS_SALARY_TABLE[grade][1]
    return None, None


def ingest_usajobs(max_pages: int = 10) -> int:
    """Fetch USAJobs postings for knowledge-work series.

    Returns number of new postings inserted.
    Returns 0 if no API key is configured.
    """
    api_key = os.environ.get("USAJOBS_API_KEY")
    user_agent = os.environ.get("USAJOBS_USER_AGENT", "job-market-tool/contact@example.com")

    if not api_key:
        print("[USAJobs] No API key found (set USAJOBS_API_KEY env var)")
        print("[USAJobs] Register at https://developer.usajobs.gov/apirequest/")
        print("[USAJobs] Skipping this source.")
        return 0

    print("[USAJobs] Fetching postings...")
    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": user_agent,
        "Authorization-Key": api_key,
        "Accept": "application/json",
    }

    all_rows = []
    total_fetched = 0

    for series in KNOWLEDGE_WORK_SERIES:
        params = {
            "JobCategoryCode": series,
            "LocationName": "United States",
            "ResultsPerPage": 500,
        }
        response = requests.get(USAJOBS_URL, headers=headers, params=params, timeout=30)

        if response.status_code != 200:
            print(f"  [USAJobs] Series {series}: HTTP {response.status_code}, skipping")
            time.sleep(1)
            continue

        data = response.json()
        search_result = data.get("SearchResult", {})
        items = search_result.get("SearchResultItems", [])
        count = search_result.get("SearchResultCount", 0)

        print(f"  [USAJobs] Series {series}: {count} results")
        total_fetched += count

        for item in items:
            job = item.get("MatchedObjectDescriptor", {})
            title = job.get("PositionTitle", "")
            dept = job.get("DepartmentName", "")
            org = job.get("OrganizationName", "")
            employer = dept if dept else org

            location_list = job.get("PositionLocationDisplay", "")
            location = location_list if isinstance(location_list, str) else ", ".join(location_list)

            # Federal jobs always have salary via GS grade
            gs_grade = (
                job.get("JobGrade", {}).get("Code", "")
                if isinstance(job.get("JobGrade"), dict)
                else job.get("JobGrade", "")
            )
            salary_min, salary_max = gs_to_salary(gs_grade if isinstance(gs_grade, str) else "")

            # Also check PositionRemuneration
            if salary_min is None:
                remuneration = job.get("PositionRemuneration", [{}])
                if remuneration and isinstance(remuneration, list):
                    r = remuneration[0]
                    try:
                        salary_min = float(r.get("MinimumRange", 0)) or None
                        salary_max = float(r.get("MaximumRange", 0)) or None
                    except (ValueError, TypeError):
                        pass

            if salary_min is None and salary_max is None:
                continue  # No salary data, skip per user requirement

            posted_str = job.get("PublicationStartDate", "")
            posted_date = None
            if posted_str:
                try:
                    posted_date = datetime.fromisoformat(posted_str).date()
                except ValueError:
                    pass

            apply_url = (
                job.get("ApplyURI", [None])[0] if isinstance(job.get("ApplyURI"), list) else None
            )

            all_rows.append(
                {
                    "source": "usajobs",
                    "employer": employer,
                    "title": title,
                    "location": location,
                    "remote": False,  # Federal jobs rarely are
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                    "salary_currency": "USD",
                    "description": job.get("QualificationSummary", "")[:5000],
                    "url": job.get("PositionURI", ""),
                    "apply_url": apply_url or job.get("PositionURI", ""),
                    "soc_code": None,
                    "posted_date": posted_date,
                    "fetched_date": date.today(),
                    "salary_disclosed": True,
                }
            )

        time.sleep(0.5)  # Be polite

    print(f"  [USAJobs] {total_fetched} total fetched, {len(all_rows)} after filters")

    con = get_db_connection()
    ensure_postings_table(con)
    inserted, skipped = insert_postings(con, all_rows)
    con.close()

    print(f"  [USAJobs] Inserted {inserted}, skipped {skipped} duplicates")
    return inserted


if __name__ == "__main__":
    ingest_usajobs()
