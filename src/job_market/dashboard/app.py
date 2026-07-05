"""Streamlit dashboard — two-tier job market tracker.

Tier 1 (left): Top occupations by growth_score with sparklines
Tier 2 (right): Postings drilldown for selected occupation

Run: streamlit run src/job_market/dashboard/app.py
"""

from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = PROJECT_ROOT / "data" / "jobs.duckdb"


@st.cache_resource
def get_db():
    """Cached DuckDB connection."""
    return duckdb.connect(str(DB_PATH), read_only=True)


def load_growth_metrics() -> pd.DataFrame:
    """Load the growth_metrics table."""
    con = get_db()
    try:
        df = con.execute("""
            SELECT soc_code, occupation_title, growth_score,
                   projected_10yr_growth_pct, median_annual_wage,
                   annual_openings,
                   component_posting, component_pay, component_proj, component_hhi
            FROM growth_metrics
            ORDER BY growth_score DESC
        """).fetchdf()
        return df
    except Exception:
        return pd.DataFrame()


def load_postings(
    search_title: str = "", salary_min: int = 0, remote_only: bool = False
) -> pd.DataFrame:
    """Load postings with optional filters."""
    con = get_db()
    try:
        query = """
            SELECT source, employer, title, location, remote,
                   salary_min, salary_max, url, apply_url, posted_date,
                   salary_disclosed
            FROM postings
            WHERE 1=1
        """
        params = []

        if search_title:
            query += " AND LOWER(title) LIKE ?"
            params.append(f"%{search_title.lower()}%")

        if salary_min > 0:
            query += " AND (salary_max >= ? OR salary_min >= ?)"
            params.extend([salary_min, salary_min])

        if remote_only:
            query += " AND remote = TRUE"

        query += " ORDER BY posted_date DESC NULLS LAST LIMIT 50"

        return con.execute(query, params).fetchdf()
    except Exception:
        return pd.DataFrame()


def format_salary(row) -> str:
    """Format salary range for display."""
    smin = row.get("salary_min")
    smax = row.get("salary_max")
    if smin and smax:
        return f"${smin:,.0f} - ${smax:,.0f}"
    elif smin:
        return f"${smin:,.0f}+"
    elif smax:
        return f"up to ${smax:,.0f}"
    return "N/A"


def main():
    st.set_page_config(
        page_title="Job Market Tracker",
        page_icon="📊",
        layout="wide",
    )

    st.title("📊 Job Market Tracker")
    st.markdown("Track trending job markets: macro growth signals + live posting drilldown")

    # Load data
    metrics_df = load_growth_metrics()

    if metrics_df.empty:
        st.warning(
            "No data found. Run the ingest scripts first:\n"
            "- `python -m job_market.ingest.bls`\n"
            "- `python -m job_market.ingest.remoteok`\n"
            "- `python -m job_market.ingest.muse`\n"
            "- `python -m job_market.metrics.growth`"
        )
        return

    # --- Data freshness badge ---
    status_path = PROJECT_ROOT / "data" / "ingest_status.json"
    if status_path.exists():
        import json

        status = json.loads(status_path.read_text())
        last_run_str = status.get("last_run_overall", "")
        if last_run_str:
            try:
                last_run = datetime.fromisoformat(last_run_str)
                days_old = (datetime.now() - last_run).days
                if days_old <= 7:
                    st.sidebar.success(f"✅ Data fresh — last updated {days_old}d ago")
                elif days_old <= 10:
                    st.sidebar.warning(f"⚠️ Data {days_old}d old — run weekly ingest")
                else:
                    st.sidebar.error(f"🔴 Data {days_old}d old — STALE. Run ingest!")
                st.sidebar.caption(f"Last run: {last_run.strftime('%Y-%m-%d %H:%M')}")
            except ValueError:
                st.sidebar.info("Last run date unreadable")
    else:
        st.sidebar.info("No ingest status yet — run the weekly script")

    # --- Sidebar filters ---
    st.sidebar.header("Filters")

    # Salary range filter
    salary_options = [0, 50000, 75000, 100000, 125000, 150000, 200000]
    salary_min = st.sidebar.select_slider(
        "Minimum Salary ($)",
        options=salary_options,
        value=0,
    )

    # Remote only toggle
    remote_only = st.sidebar.checkbox("Remote only", value=False)

    # Occupation search
    occupation_search = st.sidebar.text_input("Search occupations", "")

    # --- Tier 1: Growth Score Table (left pane) ---
    st.header("Tier 1: Top Growing Occupations")

    # Filter by search
    display_df = metrics_df.copy()
    if occupation_search:
        display_df = display_df[
            display_df["occupation_title"].str.contains(occupation_search, case=False, na=False)
        ]

    # Show top 50
    display_df = display_df.head(50)

    # Format for display
    display_df["Median Wage"] = display_df["median_annual_wage"].apply(
        lambda x: f"${x:,.0f}" if pd.notna(x) else "N/A"
    )
    display_df["10yr Growth %"] = display_df["projected_10yr_growth_pct"].apply(
        lambda x: f"{x:.1f}%" if pd.notna(x) else "N/A"
    )
    display_df["Score"] = display_df["growth_score"].apply(lambda x: f"{x:.4f}")
    display_df["Annual Openings"] = display_df["annual_openings"].apply(
        lambda x: f"{x:,.0f}" if pd.notna(x) else "N/A"
    )

    # Select columns for display
    show_cols = [
        "soc_code",
        "occupation_title",
        "Score",
        "10yr Growth %",
        "Median Wage",
        "Annual Openings",
    ]
    col_titles = {
        "soc_code": "SOC Code",
        "occupation_title": "Occupation",
    }

    st.dataframe(
        display_df[show_cols].rename(columns=col_titles),
        use_container_width=True,
        height=400,
        hide_index=True,
    )

    # --- Component breakdown chart ---
    st.subheader("Score Component Breakdown (Top 20)")
    top20 = display_df.head(20)

    # Create a bar chart of component scores
    import plotly.graph_objects as go

    fig = go.Figure()
    for component, label, color in [
        ("component_posting", "Posting Volume", "#1f77b4"),
        ("component_pay", "Pay Level", "#ff7f0e"),
        ("component_proj", "10yr Projection", "#2ca02c"),
        ("component_hhi", "Employer Diversity", "#d62728"),
    ]:
        if component in top20.columns:
            fig.add_trace(
                go.Bar(
                    x=top20["occupation_title"],
                    y=top20[component],
                    name=label,
                    marker_color=color,
                )
            )

    fig.update_layout(
        barmode="stack",
        xaxis_title="Occupation",
        yaxis_title="Component Score",
        xaxis_tickangle=-45,
        height=400,
        showlegend=True,
    )
    st.plotly_chart(fig, use_container_width=True)

    # --- Tier 2: Postings Drilldown (right pane) ---
    st.header("Tier 2: Live Postings Drilldown")

    # Let user pick an occupation to drill down
    selected_occupation = st.selectbox(
        "Select an occupation to see live postings",
        options=[""] + metrics_df["occupation_title"].tolist(),
        index=0,
    )

    if selected_occupation:
        st.markdown(f"**Postings matching: {selected_occupation}**")

        # Search postings by title keyword from the occupation
        # Use first word of occupation as search term
        search_term = selected_occupation.split(",")[0].split("(")[0].strip()
        postings_df = load_postings(
            search_title=search_term,
            salary_min=salary_min,
            remote_only=remote_only,
        )

        if postings_df.empty:
            st.info(
                f"No postings found matching '{search_term}'. "
                "Try a broader search or run the ingest scripts to fetch more postings."
            )
        else:
            st.success(f"Found {len(postings_df)} postings")

            # Format for display
            postings_df["Salary Range"] = postings_df.apply(format_salary, axis=1)
            postings_df["Remote"] = postings_df["remote"].apply(lambda x: "Yes" if x else "No")
            postings_df["Source"] = postings_df["source"].str.upper()

            display_postings = postings_df[
                [
                    "source",
                    "employer",
                    "title",
                    "location",
                    "Remote",
                    "Salary Range",
                    "posted_date",
                    "url",
                ]
            ].copy()
            display_postings.columns = [
                "Source",
                "Employer",
                "Title",
                "Location",
                "Remote",
                "Salary",
                "Posted",
                "URL",
            ]

            # Make URL clickable
            st.dataframe(
                display_postings,
                use_container_width=True,
                height=400,
                hide_index=True,
                column_config={
                    "URL": st.column_config.LinkColumn("Apply Link"),
                },
            )
    else:
        st.info("👆 Select an occupation above to see live job postings with salaries.")

    # --- Export section ---
    st.header("Export Data")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("📥 Export Growth Metrics (CSV)"):
            metrics_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download growth_metrics.csv",
                data=metrics_df.to_csv(index=False).encode("utf-8"),
                file_name="growth_metrics.csv",
                mime="text/csv",
            )
    with col2:
        if st.button("📥 Export Postings (CSV)"):
            all_postings = load_postings()
            if not all_postings.empty:
                st.download_button(
                    "Download postings.csv",
                    data=all_postings.to_csv(index=False).encode("utf-8"),
                    file_name="postings.csv",
                    mime="text/csv",
                )
            else:
                st.info("No postings to export")

    # --- Data source summary ---
    st.sidebar.markdown("---")
    st.sidebar.header("Data Sources")
    con = get_db()
    try:
        bls_count = con.execute("SELECT COUNT(*) FROM bls_oews").fetchone()[0]
        emp_count = con.execute("SELECT COUNT(*) FROM bls_emp_projections").fetchone()[0]
        post_count = con.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
        metrics_count = con.execute("SELECT COUNT(*) FROM growth_metrics").fetchone()[0]
        st.sidebar.metric("BLS OEWS Occupations", bls_count)
        st.sidebar.metric("BLS Emp Projections", emp_count)
        st.sidebar.metric("Growth Metrics", metrics_count)
        st.sidebar.metric("Job Postings", post_count)

        # Source breakdown
        source_counts = con.execute("""
            SELECT source, COUNT(*) as count
            FROM postings
            GROUP BY source
            ORDER BY count DESC
        """).fetchdf()
        st.sidebar.markdown("**Postings by Source:**")
        for _, row in source_counts.iterrows():
            st.sidebar.caption(f"  {row['source']}: {row['count']}")
    except Exception:
        st.sidebar.warning("Database tables not fully populated")
    finally:
        pass


if __name__ == "__main__":
    main()
