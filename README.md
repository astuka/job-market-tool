# Job Market Tool

Two-tier job market tracker:
- **Tier 1:** Macro growth signals (BLS data) — which occupations are growing in openings and pay
- **Tier 2:** Live job posting drilldown — actual postings you can apply to today

## Quick start

```bash
# Clone
git clone https://github.com/astuka/job-market-tool.git
cd job-market-tool

# Create virtual environment
uv venv .venv --python 3.11
source .venv/Scripts/activate  # Windows
# or: source .venv/bin/activate  # Linux/Mac

# Install
uv pip install -e ".[dev]"

# Copy .env.example to .env and fill in API keys
cp .env.example .env
# Edit .env with your Adzuna API credentials

# Run initial data ingest
python -m job_market.ingest.run_weekly

# Launch dashboard
streamlit run src/job_market/dashboard/app.py
```

## API Keys

| Source | Required? | Register |
|--------|-----------|----------|
| BLS | No (optional for higher limits) | https://data.bls.gov/registrationEngine/ |
| The Muse | No | — |
| RemoteOK | No | — |
| USAJobs | Yes (free) | https://developer.usajobs.gov/apirequest/ |
| Adzuna | Yes (free) | https://developer.adzuna.com/signup |

## Configuration

Edit `config.yaml` for:
- growth_score weights (default: 30/30/25/15)
- Location filter (default: San Francisco/remote)
- Refresh cadence (default: weekly)
- SOC code prefixes for knowledge-work filter

## Dashboard

```bash
streamlit run src/job_market/dashboard/app.py
```

- **Tier 1:** Sortable table of top 50 occupations by growth_score
- **Tier 2:** Select an occupation → live postings with salaries + apply links
- **Filters:** Salary slider, remote-only toggle, occupation search
- **Export:** CSV download for growth metrics and postings
- **Freshness badge:** Green (<7d), Yellow (8-10d), Red (stale)

## REST API (for Next.js v2)

```bash
uvicorn job_market.api:app --port 8000
```

| Endpoint | Description |
|----------|-------------|
| `GET /api/roles` | Top occupations by growth_score |
| `GET /api/postings` | Live postings with filters |
| `GET /api/postings/{soc_code}` | Postings by SOC code |
| `GET /api/categories` | 12-month salary trends by category |
| `GET /api/status` | Ingest freshness status |
| `GET /api/export/roles` | CSV download |
| `GET /api/export/postings` | CSV download |

## Weekly automation

```bash
# Run manually
python -m job_market.ingest.run_weekly

# Or import Windows Task Scheduler XML
schtasks /create /tn "JobMarketTool_Weekly" /xml scripts/weekly_task.xml
```

Runs every Sunday at 6am:
1. BLS OEWS (wages) + Employment Projections (10-year growth)
2. RemoteOK + The Muse + USAJobs postings
3. Adzuna search + 12-month salary history
4. Recompute growth_score

## Data sources

| Source | API Key | Data |
|--------|---------|------|
| BLS OEWS | No | Annual wages per occupation (609 knowledge-work roles) |
| BLS Emp Projections | No | 10-year employment growth forecasts (376 roles) |
| Adzuna | Yes | Live postings + 12-month salary history (8 categories) |
| The Muse | No | Live postings (salary extracted from descriptions) |
| RemoteOK | No | Remote dev postings |
| USAJobs | Yes | Federal postings with GS-grade salaries |

## Architecture

- **Backend:** Python 3.11+ · FastAPI · DuckDB (columnar, serverless)
- **Frontend:** Streamlit (MVP) → Next.js + Recharts (v2)
- **Automation:** Windows Task Scheduler (weekly)
- **Storage:** Single DuckDB file at `data/jobs.duckdb`

## Tables

- `bls_oews` — occupation code, title, employment, mean/median wage
- `bls_emp_projections` — occupation code, title, 2024/2034 employment, growth %, openings
- `postings` — source, employer, title, location, salary range, URL, dates, dedup hash
- `adzuna_history` — category, month, avg_salary (12-month trends)
- `growth_metrics` — soc_code, occupation_title, growth_score, component scores
