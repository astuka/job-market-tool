# Job Market Tool

Two-tier job market tracker:
- **Tier 1:** Macro growth signals (BLS data) — which occupations are growing in openings and pay
- **Tier 2:** Live job posting drilldown — actual postings you can apply to today

## Quick start

```bash
# Install
uv pip install -e ".[dev]"

# Run initial BLS ingest
python -m job_market.ingest.bls

# Launch dashboard
streamlit run src/job_market/dashboard/app.py
```

## Configuration

Edit `config.yaml` for:
- Data source settings
- growth_score weights (default: 30/30/25/15)
- Location filter (default: San Francisco/remote)
- Refresh cadence (default: weekly)

## Data sources

| Source | API Key | Tier |
|--------|---------|------|
| BLS (OEWS, Employment Projections) | No (optional for higher limits) | Macro |
| The Muse | No | Postings |
| RemoteOK | No | Postings |
| USAJobs | No (User-Agent only) | Postings |
| Adzuna | Yes | Postings + History |

## Architecture

- **Backend:** Python + FastAPI
- **Storage:** DuckDB (single file, project-local)
- **Frontend:** Streamlit (MVP) → Next.js (v2)
- **Automation:** Windows Task Scheduler (weekly)
