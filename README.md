# CTA TransitPulse

End-to-end Chicago Transit Authority bus analytics built with Python, SQL, Pandas, SQLite, Streamlit, and Plotly.

TransitPulse turns vehicle-location and arrival-prediction records into route-level KPIs, reliability trends, operating-period comparisons, activity heatmaps, geographic exploration, and downloadable route summaries.

**Live dashboard:** [cta-transitpulse.streamlit.app](https://cta-transitpulse-m2nssudw6zppycqfwjega2.streamlit.app/)

## Project snapshot

| | What it delivers |
|---|---|
| Problem | Raw transit observations are difficult to use for operational decisions. |
| Data model | Raw ingestion tables plus processed analytical tables in SQLite. |
| Scale | 17K+ vehicle observations, 69K+ predictions, and 14 CTA routes in the included demo profile. |
| Analytics | 10 reusable SQL analyses in [`src/queries.sql`](src/queries.sql). |
| Product | Interactive Streamlit dashboard with route, time-window, KPI, heatmap, map, and CSV export views. |

> The no-key demo dataset is deterministic synthetic data shaped around CTA bus operating patterns. Live CTA API mode is optional and documented below.

## Live demo

The hosted Streamlit dashboard starts with a reproducible seven-day demo dataset, so no API key is required to explore the project.

[Open CTA TransitPulse](https://cta-transitpulse-m2nssudw6zppycqfwjega2.streamlit.app/)

## Dashboard

The dashboard includes:

- vehicle observations, average delay, on-time performance, active-route, and headway KPIs;
- reliability trend by day;
- busiest-route ranking;
- hour × day activity heatmap;
- route delay and on-time comparison, including peak vs off-peak;
- latest vehicle geography on an OpenStreetMap basemap;
- route KPI detail table with CSV download;
- recent service-alert panel.

The included screenshot is available at [`data/processed/dashboard_screenshot.png`](data/processed/dashboard_screenshot.png).

## Quick start

Requires Python 3.10+.

```bash
git clone <your-github-repo-url>
cd cta-analytics

python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
python -m src.pipeline --sample --days 7 --reset
streamlit run src/dashboard.py
```

Open <http://localhost:8501>. The pipeline command performs the full workflow:

1. generates deterministic demo records;
2. stores raw routes, vehicles, predictions, and alerts in SQLite;
3. cleans and enriches the data with Pandas;
4. writes `vehicles_processed`, `predictions_processed`, and `route_stats`;
5. makes the dashboard immediately available.

To use the installed CLI entry point instead:

```bash
pip install -e .
cta-pipeline --sample --days 7 --reset
```

## Live CTA API mode

1. Request CTA developer credentials from the [CTA developer portal](https://www.transitchicago.com/developers/bustracker.aspx).
2. Copy `.env.example` to `.env` and add `CTA_BUS_API_KEY`.
3. Run one ingestion and processing cycle:

```bash
python -m src.pipeline --once
```

For repeated collection, run the ingestion process separately and rebuild the analytical tables after each collection cycle:

```bash
python src/ingestion.py --interval 60
python src/processing.py
```

The train key is retained in `.env.example` for future train-data expansion; the current dashboard is focused on bus routes.

## Architecture

```text
CTA Bus Tracker API / demo generator
                │
                ▼
       raw_* SQLite tables
                │
                ▼
  Pandas cleaning + feature engineering
                │
                ▼
vehicles_processed · predictions_processed · route_stats
                │
                ├── 10 analytical SQL queries
                └── Streamlit + Plotly dashboard
```

### Analytical model

The database uses a practical star-style layout:

- `raw_routes`: route reference data and CTA colors;
- `raw_vehicles`: point-in-time vehicle observations and coordinates;
- `raw_predictions`: stop-level predicted arrivals;
- `raw_alerts`: service bulletins and severity;
- `vehicles_processed`: temporal flags, delay metrics, and headway features;
- `predictions_processed`: normalized arrival-prediction features;
- `route_stats`: daily/hourly route rollups for KPI reporting.

Peak hours are weekday 07:00–09:59 and 16:00–18:59. On-time performance uses the source delay flag; the sample generator marks records with more than three minutes of delay as late.

## SQL analysis library

[`src/queries.sql`](src/queries.sql) contains ten stakeholder-facing queries:

1. delay by route;
2. busiest routes by hour;
3. peak vs off-peak on-time performance;
4. alert frequency and severity;
5. peak vs off-peak headway;
6. most delayed stops;
7. day-of-week patterns;
8. daily reliability trend;
9. hour × day heatmap source;
10. route-level executive summary.

Run the file with any SQLite client after generating data:

```bash
sqlite3 data/cta_data.db < src/queries.sql
```

## Development checks

Install development dependencies and run the test suite:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q src
```

The GitHub Actions workflow runs those checks on Python 3.10, 3.11, and 3.12.

## Repository layout

```text
cta-analytics/
├── .github/workflows/ci.yml
├── data/
│   ├── README.md
│   ├── raw/.gitkeep
│   └── processed/dashboard_screenshot.png
├── notebooks/01_eda.ipynb
├── src/
│   ├── dashboard.py
│   ├── ingestion.py
│   ├── pipeline.py
│   ├── processing.py
│   └── queries.sql
├── tests/test_pipeline.py
├── .env.example
├── .gitignore
├── LICENSE
├── pyproject.toml
├── requirements.txt
└── requirements-dev.txt
```

## License

MIT. See [`LICENSE`](LICENSE).
