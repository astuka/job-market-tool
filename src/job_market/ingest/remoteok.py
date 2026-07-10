"""RemoteOK job posting ingest.

Fetches from https://remoteok.com/api — free, no key required.
Returns a single JSON array of all remote postings.

All knowledge-work postings are ingested regardless of salary availability.
When salary is present in the API response it is stored; otherwise salary
fields are null and salary_disclosed is False.
"""

from datetime import date, datetime

import requests

from .db import ensure_postings_table, get_db_connection, insert_postings

REMOTEOK_URL = "https://remoteok.com/api"

# Knowledge-work keywords to filter on (title contains any of these)
KNOWLEDGE_WORK_KEYWORDS = [
    "developer",
    "engineer",
    "designer",
    "manager",
    "analyst",
    "architect",
    "scientist",
    "consultant",
    "specialist",
    "director",
    "lead",
    "writer",
    "editor",
    "marketer",
    "strategist",
    "researcher",
    "data",
    "product",
    "program",
    "project",
    "operations",
    "finance",
    "account",
    "advisor",
    "coordinator",
    "administrator",
    "officer",
    "planner",
    "supervisor",
]

# RemoteOK tags that indicate knowledge work
KNOWLEDGE_WORK_TAGS = [
    "programming",
    "devops",
    "design",
    "customer support",
    "sales",
    "marketing",
    "product",
    "finance",
    "writing",
    "management",
    "recruiter",
    "data",
    "ai",
    "ml",
    "crypto",
    "frontend",
    "backend",
    "fullstack",
    "mobile",
    "dev",
    "software",
    "cloud",
    "security",
]


def is_knowledge_work(title: str, tags: list) -> bool:
    """Check if a posting is knowledge-work based on title and tags."""
    title_lower = title.lower()
    if any(kw in title_lower for kw in KNOWLEDGE_WORK_KEYWORDS):
        return True
    if tags:
        tags_lower = [t.lower() for t in tags]
        if any(tw in tags_lower for tw in KNOWLEDGE_WORK_TAGS):
            return True
    return False


def ingest_remoteok() -> int:
    """Fetch RemoteOK postings, filter to salary-disclosed + knowledge-work.

    Returns number of new postings inserted.
    """
    print("[RemoteOK] Fetching postings...")
    headers = {
        "User-Agent": "job-market-tool/0.1 (https://github.com/astuka/job-market-tool)",
        "Accept": "application/json",
    }

    response = requests.get(REMOTEOK_URL, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()

    # First element is metadata, rest are jobs
    jobs = [item for item in data if "id" in item and "position" in item]
    print(f"  [RemoteOK] {len(jobs)} total postings fetched")

    # Filter to knowledge-work only — no salary filter
    rows = []
    for job in jobs:
        salary_min = job.get("salary_min", 0) or 0
        salary_max = job.get("salary_max", 0) or 0
        has_salary = salary_min > 0 or salary_max > 0

        title = job.get("position", "")
        tags = job.get("tags", [])

        if not is_knowledge_work(title, tags):
            continue

        # Parse date
        epoch = job.get("epoch") or job.get("date")
        posted_date = None
        if isinstance(epoch, (int, float)):
            posted_date = datetime.fromtimestamp(epoch).date()
        elif isinstance(epoch, str) and "T" in epoch:
            try:
                posted_date = datetime.fromisoformat(epoch).date()
            except ValueError:
                pass

        url = job.get("url", "")
        apply_url = job.get("apply_url") or url

        rows.append(
            {
                "source": "remoteok",
                "employer": job.get("company", ""),
                "title": title,
                "location": "Remote" if not job.get("location") else job.get("location", "Remote"),
                "remote": True,
                "salary_min": float(salary_min) if salary_min else None,
                "salary_max": float(salary_max) if salary_max else None,
                "salary_currency": "USD",
                "description": job.get("description", "")[:5000],
                "url": url,
                "apply_url": apply_url,
                "soc_code": None,
                "posted_date": posted_date,
                "fetched_date": date.today(),
                "salary_disclosed": has_salary,
            }
        )

    print(f"  [RemoteOK] {len(rows)} postings after filters (knowledge-work only)")

    con = get_db_connection()
    ensure_postings_table(con)
    inserted, skipped = insert_postings(con, rows)
    con.close()

    print(f"  [RemoteOK] Inserted {inserted}, skipped {skipped} duplicates")
    return inserted


if __name__ == "__main__":
    ingest_remoteok()
