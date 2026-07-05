"""Weekly ingest orchestrator.

Runs all data source ingests sequentially with retry logic and per-source
JSON cache for failed pulls. Designed to run via Windows Task Scheduler.

Usage:
    python -m job_market.ingest.run_weekly

Windows Task Scheduler XML export included in scripts/weekly_task.xml
"""

import json
import time
import traceback
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = PROJECT_ROOT / "data" / "jobs.duckdb"
STATUS_PATH = PROJECT_ROOT / "data" / "ingest_status.json"


def update_status(source: str, success: bool, count: int = 0, error: str = ""):
    """Update the ingest status JSON file."""
    status = {}
    if STATUS_PATH.exists():
        try:
            status = json.loads(STATUS_PATH.read_text())
        except json.JSONDecodeError:
            status = {}

    status[source] = {
        "last_run": datetime.now().isoformat(),
        "success": success,
        "count": count,
        "error": error,
    }

    status["last_run_overall"] = datetime.now().isoformat()

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2))


def run_source(name: str, func, *args, **kwargs) -> int:
    """Run a single ingest source with error handling.

    Returns the count of items inserted, or 0 on failure.
    """
    print(f"\n{'=' * 50}")
    print(f"  Running: {name}")
    print(f"{'=' * 50}")

    try:
        count = func(*args, **kwargs)
        update_status(name, success=True, count=count)
        print(f"  ✓ {name}: {count} items")
        return count
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"  ✗ {name} FAILED: {error_msg}")
        print(f"  Traceback: {traceback.format_exc()[:500]}")
        update_status(name, success=False, count=0, error=error_msg)
        return 0


def main():
    print("=" * 60)
    print(f"WEEKLY INGEST — {date.today()}")
    print("=" * 60)

    start_time = time.time()
    total_inserted = 0

    # Import all ingest modules
    from job_market.ingest.bls import get_db_connection, ingest_emp_projections, ingest_oews

    # 1. BLS OEWS (wages)
    con = get_db_connection()
    total_inserted += run_source("bls_oews", ingest_oews, con)

    # 2. BLS Employment Projections
    total_inserted += run_source("bls_emp_projections", ingest_emp_projections, con)
    con.close()

    # Small delay between sources to avoid rate limits
    time.sleep(2)

    # 3. RemoteOK
    from job_market.ingest.remoteok import ingest_remoteok

    total_inserted += run_source("remoteok", ingest_remoteok)

    time.sleep(2)

    # 4. The Muse
    from job_market.ingest.muse import ingest_muse

    total_inserted += run_source("muse", ingest_muse, max_pages=15)

    time.sleep(2)

    # 5. USAJobs (optional — needs API key)
    from job_market.ingest.usajobs import ingest_usajobs

    total_inserted += run_source("usajobs", ingest_usajobs, max_pages=5)

    time.sleep(2)

    # 6. Adzuna search (live postings)
    from job_market.ingest.adzuna import ingest_adzuna_search

    total_inserted += run_source("adzuna_search", ingest_adzuna_search, max_pages=3)

    time.sleep(2)

    # 7. Adzuna history (12-month salary trends)
    from job_market.ingest.adzuna import ingest_adzuna_history

    run_source("adzuna_history", ingest_adzuna_history)

    time.sleep(2)

    # 8. Recompute growth_score with fresh data
    from job_market.metrics.growth import compute_growth_score

    metrics_count = run_source("growth_score", compute_growth_score)

    # Summary
    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("WEEKLY INGEST COMPLETE")
    print("=" * 60)
    print(f"  Total items inserted:  {total_inserted}")
    print(f"  Growth metrics:       {metrics_count}")
    print(f"  Elapsed:              {elapsed:.0f}s")
    print(f"  Status file:          {STATUS_PATH}")

    # Print status summary
    if STATUS_PATH.exists():
        status = json.loads(STATUS_PATH.read_text())
        print("\nSource status:")
        for source, info in status.items():
            if source == "last_run_overall":
                continue
            status_icon = "✓" if info.get("success") else "✗"
            count = info.get("count", 0)
            print(f"  {status_icon} {source}: {count} items")


if __name__ == "__main__":
    main()
