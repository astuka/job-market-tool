"""The Muse job posting ingest.

Fetches from https://www.themuse.com/api/public/jobs — free, no key required (500 req/hr).
Paginated, 20 results per page.

All knowledge-work postings are ingested regardless of salary availability.
When salary is mentioned in the description, it is extracted via regex and
salary_disclosed is set to True; otherwise salary fields are null and
salary_disclosed is False.
"""

import re
import time
from datetime import date, datetime

import requests

from .db import ensure_postings_table, get_db_connection, insert_postings

MUSE_URL = "https://www.themuse.com/api/public/jobs"

# Knowledge-work categories on The Muse
KNOWLEDGE_WORK_CATEGORIES = [
    "Engineering",
    "Tech",
    "IT",
    "Data",
    "Design",
    "Product",
    "Marketing",
    "Finance",
    "Operations",
    "Sales",
    "Business",
    "Strategy",
    "Consulting",
    "Content",
    "HR",
    "Legal",
    "Research",
    "Analytics",
]

# Salary patterns in job descriptions
SALARY_PATTERNS = [
    # $80,000 - $120,000  or  $80k - $120k
    re.compile(r"\$(\d[\d,]*)\s*[kK]?\s*[-–to]\s*\$(\d[\d,]*)\s*[kK]?", re.I),
    # $80,000 to $120,000
    re.compile(r"\$(\d[\d,]*)\s*[kK]?\s*(?:to|through)\s*\$(\d[\d,]*)\s*[kK]?", re.I),
    # Salary: $80,000
    re.compile(r"(?:salary|compensation|pay)[:\s]+\$?(\d[\d,]*)\s*[kK]?", re.I),
    # $80k-$120k
    re.compile(r"\$(\d+)k\s*[-–]\s*\$(\d+)k", re.I),
]


def parse_salary(text: str) -> tuple:
    """Extract salary range from job description text.

    Returns (min, max) or (None, None) if not found.
    """
    if not text:
        return None, None

    for pattern in SALARY_PATTERNS:
        match = pattern.search(text)
        if match:
            groups = match.groups()
            if len(groups) >= 2 and groups[1]:
                min_val = float(groups[0].replace(",", "")) * (
                    1000 if "k" in match.group(0).lower() else 1
                )
                max_val = float(groups[1].replace(",", "")) * (
                    1000 if "k" in match.group(0).lower() else 1
                )
                return min_val, max_val
            elif len(groups) >= 1 and groups[0]:
                val = float(groups[0].replace(",", "")) * (
                    1000 if "k" in match.group(0).lower() else 1
                )
                return val, val

    return None, None


def is_knowledge_work(categories: list, levels: list, title: str) -> bool:
    """Check if posting is knowledge-work via categories, levels, or title."""
    if categories:
        cats_lower = [c.lower() for c in categories]
        if any(kw in " ".join(cats_lower) for kw in [c.lower() for c in KNOWLEDGE_WORK_CATEGORIES]):
            return True
    # Broad check: if it has categories at all, it's likely white-collar
    if categories:
        return True
    # Fallback: check title
    title_lower = title.lower()
    knowledge_keywords = [
        "manager",
        "director",
        "engineer",
        "developer",
        "designer",
        "analyst",
        "strategist",
        "specialist",
        "coordinator",
        "architect",
        "scientist",
        "writer",
        "editor",
        "marketer",
        "researcher",
        "consultant",
        "advisor",
        "officer",
        "planner",
        "lead",
        "head",
        "vp",
        "chief",
    ]
    return any(kw in title_lower for kw in knowledge_keywords)


def ingest_muse(max_pages: int = 20) -> int:
    """Fetch Muse postings — all knowledge-work, no salary filter.

    Salary is extracted from the description when available but is not required.

    Args:
        max_pages: Maximum number of pages to fetch (20 results per page).

    Returns number of new postings inserted.
    """
    print("[Muse] Fetching postings...")
    headers = {
        "User-Agent": "job-market-tool/0.1 (https://github.com/astuka/job-market-tool)",
        "Accept": "application/json",
    }

    all_rows = []
    total_fetched = 0

    for page in range(0, max_pages):
        params = {"page": page}
        response = requests.get(MUSE_URL, headers=headers, params=params, timeout=30)

        if response.status_code == 403:
            print(f"  [Muse] Rate limited on page {page}, stopping")
            break

        response.raise_for_status()
        data = response.json()

        results = data.get("results", [])
        if not results:
            print(f"  [Muse] No results on page {page}, stopping")
            break

        total_fetched += len(results)
        page_count = data.get("page_count", "?")
        print(f"  [Muse] Page {page}/{page_count}: {len(results)} results")

        for job in results:
            categories = [c.get("name", "") for c in job.get("categories", [])]
            levels = [lv.get("name", "") for lv in job.get("levels", [])]
            locations = [loc.get("name", "") for loc in job.get("locations", [])]
            title = job.get("name", "")

            if not is_knowledge_work(categories, levels, title):
                continue

            # Extract salary from description (optional — not required)
            description = job.get("contents", "")
            salary_min, salary_max = parse_salary(description)
            has_salary = salary_min is not None and salary_max is not None

            # Location handling
            location_str = ", ".join(locations) if locations else "Remote"
            is_remote = any("remote" in loc.lower() for loc in locations) or not locations

            # Parse date
            pub_date_str = job.get("publication_date", "")
            posted_date = None
            if pub_date_str:
                try:
                    posted_date = datetime.fromisoformat(pub_date_str.replace("Z", "")).date()
                except ValueError:
                    pass

            company = (
                job.get("company", {}).get("name", "")
                if isinstance(job.get("company"), dict)
                else ""
            )
            url = f"https://www.themuse.com/jobs/{job.get('id', '')}"

            all_rows.append(
                {
                    "source": "muse",
                    "employer": company,
                    "title": title,
                    "location": location_str,
                    "remote": is_remote,
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                    "salary_currency": "USD",
                    "description": description[:5000] if description else "",
                    "url": url,
                    "apply_url": job.get("apply_url", url),
                    "soc_code": None,
                    "posted_date": posted_date,
                    "fetched_date": date.today(),
                    "salary_disclosed": has_salary,
                }
            )

        # Be polite — small delay between pages
        time.sleep(0.5)

    print(
        f"  [Muse] {total_fetched} total fetched, {len(all_rows)} after filters (knowledge-work only)"
    )

    con = get_db_connection()
    ensure_postings_table(con)
    inserted, skipped = insert_postings(con, all_rows)
    con.close()

    print(f"  [Muse] Inserted {inserted}, skipped {skipped} duplicates")
    return inserted


if __name__ == "__main__":
    ingest_muse()
