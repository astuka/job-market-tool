"""FastAPI read-only API serving DuckDB queries.

Provides REST endpoints for the Next.js v2 dashboard:
- GET /api/roles — top occupations by growth_score
- GET /api/postings/{soc_code} — live postings for an occupation
- GET /api/categories — Adzuna category salary trends
- GET /api/status — ingest status and data freshness

Run: uvicorn job_market.api:app --reload --port 8000
"""

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "jobs.duckdb"
STATUS_PATH = PROJECT_ROOT / "data" / "ingest_status.json"

app = FastAPI(
    title="Job Market Tracker API",
    description="Read-only API for job market growth data and live postings",
    version="1.0.0",
)

# CORS for Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    return duckdb.connect(str(DB_PATH), read_only=True)


@app.get("/api/roles")
def get_roles(
    limit: int = Query(50, ge=1, le=500),
    sort_by: str = Query(
        "growth_score", pattern="^(growth_score|median_annual_wage|projected_10yr_growth_pct)$"
    ),
    min_salary: Optional[float] = Query(None, ge=0),
):
    """Get top occupations by growth_score or other metric."""
    con = get_db()
    try:
        query = """
            SELECT soc_code, occupation_title, growth_score,
                   projected_10yr_growth_pct, median_annual_wage,
                   annual_openings,
                   component_posting, component_pay, component_proj, component_hhi
            FROM growth_metrics
        """
        params = []
        if min_salary:
            query += " WHERE median_annual_wage >= ?"
            params.append(min_salary)
        query += f" ORDER BY {sort_by} DESC LIMIT ?"
        params.append(limit)

        df = con.execute(query, params).fetchdf()
        return df.to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()


@app.get("/api/postings")
def get_postings(
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    salary_min: Optional[float] = Query(None, ge=0),
    remote_only: bool = Query(False),
    source: Optional[str] = Query(None),
):
    """Get live job postings with filters."""
    con = get_db()
    try:
        query = """
            SELECT source, employer, title, location, remote,
                   salary_min, salary_max, url, apply_url, posted_date,
                   salary_disclosed
            FROM postings WHERE 1=1
        """
        params = []

        if search:
            query += " AND LOWER(title) LIKE ?"
            params.append(f"%{search.lower()}%")

        if salary_min:
            query += " AND (salary_max >= ? OR salary_min >= ?)"
            params.extend([salary_min, salary_min])

        if remote_only:
            query += " AND remote = TRUE"

        if source:
            query += " AND source = ?"
            params.append(source.lower())

        query += " ORDER BY posted_date DESC NULLS LAST LIMIT ?"
        params.append(limit)

        df = con.execute(query, params).fetchdf()
        return df.to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()


@app.get("/api/postings/{soc_code}")
def get_postings_by_soc(soc_code: str, limit: int = Query(20, ge=1, le=100)):
    """Get postings matching a SOC code prefix."""
    con = get_db()
    try:
        df = con.execute(
            """
            SELECT source, employer, title, location, remote,
                   salary_min, salary_max, url, apply_url, posted_date
            FROM postings
            WHERE salary_disclosed = TRUE
            ORDER BY posted_date DESC NULLS LAST
            LIMIT ?
            """,
            [limit],
        ).fetchdf()
        return df.to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()


@app.get("/api/categories")
def get_category_trends():
    """Get Adzuna salary history by category (12-month trends)."""
    con = get_db()
    try:
        df = con.execute("""
            SELECT category, month, avg_salary
            FROM adzuna_history
            ORDER BY category, month
        """).fetchdf()
        return df.to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()


@app.get("/api/status")
def get_status():
    """Get ingest status and data freshness."""
    if not STATUS_PATH.exists():
        return {"status": "no_data", "message": "No ingest has been run yet"}

    status = json.loads(STATUS_PATH.read_text())
    last_run_str = status.get("last_run_overall", "")

    if last_run_str:
        last_run = datetime.fromisoformat(last_run_str)
        days_old = (datetime.now() - last_run).days
        status["days_old"] = days_old
        status["fresh"] = days_old <= 7

    return status


@app.get("/api/export/roles")
def export_roles_csv():
    """Export growth_metrics as CSV download."""
    con = get_db()
    try:
        df = con.execute("""
            SELECT soc_code, occupation_title, growth_score,
                   projected_10yr_growth_pct, median_annual_wage,
                   annual_openings
            FROM growth_metrics
            ORDER BY growth_score DESC
        """).fetchdf()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(df.columns.tolist())
        for _, row in df.iterrows():
            writer.writerow(row.tolist())

        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=growth_metrics.csv"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()


@app.get("/api/export/postings")
def export_postings_csv():
    """Export all postings as CSV download."""
    con = get_db()
    try:
        df = con.execute("""
            SELECT source, employer, title, location, remote,
                   salary_min, salary_max, url, posted_date, salary_disclosed
            FROM postings
            ORDER BY posted_date DESC NULLS LAST
        """).fetchdf()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(df.columns.tolist())
        for _, row in df.iterrows():
            writer.writerow(row.tolist())

        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=postings.csv"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()


@app.get("/")
def root():
    return {
        "name": "Job Market Tracker API",
        "version": "1.0.0",
        "endpoints": [
            "/api/roles",
            "/api/postings",
            "/api/postings/{soc_code}",
            "/api/categories",
            "/api/status",
            "/api/export/roles",
            "/api/export/postings",
        ],
    }
