"""Common database utilities for job posting ingest.

Creates and manages the `postings` table in DuckDB with dedup logic.
"""

import hashlib
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = PROJECT_ROOT / "data" / "jobs.duckdb"


def get_db_connection() -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH))


def ensure_postings_table(con: duckdb.DuckDBPyConnection):
    """Create the postings table if it doesn't exist."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS postings (
            source VARCHAR,
            employer VARCHAR,
            title VARCHAR,
            location VARCHAR,
            remote BOOLEAN,
            salary_min DOUBLE,
            salary_max DOUBLE,
            salary_currency VARCHAR,
            description TEXT,
            url VARCHAR,
            apply_url VARCHAR,
            soc_code VARCHAR,
            posted_date DATE,
            fetched_date DATE,
            posting_hash VARCHAR,
            salary_disclosed BOOLEAN
        )
    """)
    # Index for dedup lookups
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_postings_hash
        ON postings(posting_hash)
    """)


def compute_hash(employer: str, title: str, location: str) -> str:
    """Compute a dedup hash from employer + title + location."""
    raw = f"{employer}|{title}|{location}".lower().strip()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def insert_postings(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> tuple[int, int]:
    """Insert postings, skipping duplicates by posting_hash.

    Returns (inserted, skipped_dups).
    """
    if not rows:
        return 0, 0

    inserted = 0
    skipped = 0

    for row in rows:
        h = compute_hash(
            row.get("employer", ""),
            row.get("title", ""),
            row.get("location", ""),
        )
        row["posting_hash"] = h

        # Check if already exists
        existing = con.execute(
            "SELECT COUNT(*) FROM postings WHERE posting_hash = ?", [h]
        ).fetchone()[0]

        if existing > 0:
            skipped += 1
            continue

        con.execute(
            """
            INSERT INTO postings (
                source, employer, title, location, remote,
                salary_min, salary_max, salary_currency,
                description, url, apply_url, soc_code,
                posted_date, fetched_date, posting_hash, salary_disclosed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            [
                row.get("source"),
                row.get("employer"),
                row.get("title"),
                row.get("location"),
                row.get("remote", False),
                row.get("salary_min"),
                row.get("salary_max"),
                row.get("salary_currency", "USD"),
                row.get("description"),
                row.get("url"),
                row.get("apply_url"),
                row.get("soc_code"),
                row.get("posted_date"),
                row.get("fetched_date"),
                row.get("posting_hash"),
                row.get("salary_disclosed", False),
            ],
        )
        inserted += 1

    return inserted, skipped
