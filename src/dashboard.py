"""Interactive Streamlit dashboard for CTA TransitPulse."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("CTA_DB_PATH", BASE_DIR / "data" / "cta_data.db"))
DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

st.set_page_config(
    page_title="CTA TransitPulse",
    page_icon="🚆",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
        .block-container { padding-top: 1.5rem; }
        div[data-testid="metric-container"] {
            background: #172033;
            border-radius: 10px;
            padding: 14px 18px;
            border-left: 4px solid #00a1de;
        }
        div[data-testid="metric-container"] label { color: #9aa8bd !important; }
        div[data-testid="metric-container"] div { color: #f6f8fb !important; }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(str(DB_PATH))


@st.cache_data(ttl=300, show_spinner=False)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    with get_connection() as connection:
        return pd.read_sql_query(sql, connection, params=params)


def ensure_database() -> bool:
    """Create the demo database automatically on a fresh cloud deployment."""
    if DB_PATH.exists():
        try:
            with get_connection() as connection:
                vehicle_count = connection.execute(
                    "SELECT COUNT(*) FROM vehicles_processed"
                ).fetchone()[0]
            if vehicle_count > 0:
                return True
        except sqlite3.Error:
            # A blank or partially-created database can exist after a restart.
            # Rebuild it below so the hosted demo is self-healing.
            pass

    try:
        from ingestion import CTAIngestion
        from processing import CTAProcessor

        with st.spinner("Creating the demo dataset for this deployment…"):
            ingestion = CTAIngestion(
                db_path=DB_PATH,
                raw_dir=DB_PATH.parent / "raw",
                processed_dir=DB_PATH.parent / "processed",
            )
            try:
                if DB_PATH.exists():
                    ingestion.reset_database()
                ingestion.generate_sample_data(days=7)
            finally:
                ingestion.close()

            processor = CTAProcessor(db_path=DB_PATH)
            try:
                processor.run()
            finally:
                processor.close()
        return True
    except Exception as exc:
        st.error(f"Demo dataset creation failed: {exc}")
        return False


def ensure_processed() -> bool:
    """Build processed tables automatically when raw data is present."""
    try:
        count = int(query("SELECT COUNT(*) AS n FROM vehicles_processed")["n"].iloc[0])
        return count > 0
    except Exception:
        pass

    if not DB_PATH.exists():
        return False

    try:
        from processing import CTAProcessor

        with st.spinner("Building analytical tables from the loaded data…"):
            processor = CTAProcessor(db_path=DB_PATH)
            processor.run()
            processor.close()
        st.cache_data.clear()
        return True
    except Exception as exc:
        st.error(f"Processing failed: {exc}")
        return False


def value_or_dash(value: object, number_format: str = ",") -> str:
    if value is None or pd.isna(value):
        return "—"
    return format(value, number_format)


def vehicle_filter(
    selected_route: str | None,
    start_ts: str,
    end_ts: str,
    column: str = "recorded_at",
) -> tuple[str, list[str]]:
    conditions = [f"{column} >= ?", f"{column} <= ?"]
    params = [start_ts, end_ts]
    if selected_route:
        conditions.append("rt = ?")
        params.append(selected_route)
    return " AND ".join(conditions), params


def chart_layout(fig: go.Figure, height: int = 320) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=0, r=0, t=35, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


if not ensure_database():
    st.stop()

if not ensure_processed():
    st.error(
        "The database has no processed vehicle rows. Run:\n\n"
        "```bash\npython -m src.pipeline --sample --days 7 --reset\n```"
    )
    st.stop()

coverage = query(
    """
    SELECT MIN(recorded_at) AS min_ts, MAX(recorded_at) AS max_ts,
           COUNT(*) AS vehicle_count
    FROM vehicles_processed
    """
).iloc[0]
min_ts = pd.to_datetime(coverage["min_ts"])
max_ts = pd.to_datetime(coverage["max_ts"])
available_days = max(1, min(30, int((max_ts - min_ts).days) + 1))

with st.sidebar:
    st.title("CTA TransitPulse")
    st.caption("Operational analytics for Chicago CTA bus service")
    st.markdown("---")

    routes_df = query(
        "SELECT rt, rtnm FROM vehicles_processed GROUP BY rt, rtnm ORDER BY CAST(rt AS INTEGER)"
    )
    route_labels = {f"Route {row.rt} · {row.rtnm}": row.rt for row in routes_df.itertuples()}
    selected_label = st.selectbox("Route", ["All routes", *route_labels.keys()])
    selected_route = route_labels.get(selected_label)
    if available_days == 1:
        days_back = 1
        st.caption("History window: 1 day")
    else:
        days_back = st.slider("History window (days)", 1, available_days, available_days)

    if st.button("Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.caption(
        f"Loaded records: {int(coverage['vehicle_count']):,}\n\n"
        f"Coverage: {min_ts:%b %d, %Y} – {max_ts:%b %d, %Y}"
    )
    st.caption("Demo mode uses deterministic synthetic data. Live mode uses the CTA Bus Tracker API.")

window_start = max_ts - pd.Timedelta(days=days_back - 1)
window_end = max_ts + pd.Timedelta(minutes=1)
start_ts = window_start.strftime("%Y-%m-%d %H:%M:%S")
end_ts = window_end.strftime("%Y-%m-%d %H:%M:%S")
where, params = vehicle_filter(selected_route, start_ts, end_ts)
title_suffix = "All routes" if selected_route is None else selected_label

st.title("Chicago CTA Transit Analytics")
st.caption(f"{title_suffix} · {window_start:%b %d} – {max_ts:%b %d, %Y}")

kpi = query(
    f"""
    SELECT COUNT(*) AS total_vehicles,
           AVG(delay_minutes) AS avg_delay,
           SUM(CASE WHEN is_delayed = 0 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0) AS pct_on_time,
           COUNT(DISTINCT rt) AS active_routes,
           AVG(headway_minutes) AS avg_headway
    FROM vehicles_processed
    WHERE {where}
    """,
    tuple(params),
).iloc[0]

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Vehicle observations", value_or_dash(kpi["total_vehicles"], ",.0f"))
col2.metric("Average delay", f"{value_or_dash(kpi['avg_delay'], ',.1f')} min")
col3.metric("On-time performance", f"{value_or_dash(kpi['pct_on_time'], ',.1f')}%")
col4.metric("Active routes", value_or_dash(kpi["active_routes"], ",.0f"))
col5.metric("Average headway", f"{value_or_dash(kpi['avg_headway'], ',.1f')} min")

st.markdown("---")

left, right = st.columns([3, 2])

with left:
    st.subheader("Reliability trend")
    trend = query(
        f"""
        SELECT DATE(recorded_at) AS service_date,
               AVG(delay_minutes) AS avg_delay,
               SUM(CASE WHEN is_delayed = 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS pct_on_time
        FROM vehicles_processed
        WHERE {where}
        GROUP BY DATE(recorded_at)
        ORDER BY service_date
        """,
        tuple(params),
    )
    if trend.empty:
        st.info("No trend data for this filter.")
    else:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=trend["service_date"], y=trend["avg_delay"], name="Average delay (min)",
            mode="lines+markers", line=dict(color="#f97316", width=2),
        ))
        fig.add_trace(go.Scatter(
            x=trend["service_date"], y=trend["pct_on_time"], name="On-time %",
            mode="lines+markers", line=dict(color="#22c55e", width=2), yaxis="y2",
        ))
        fig.update_layout(
            yaxis=dict(title="Delay (min)"),
            yaxis2=dict(title="On-time %", overlaying="y", side="right"),
            legend=dict(orientation="h", y=-0.2),
        )
        st.plotly_chart(chart_layout(fig), use_container_width=True)

with right:
    st.subheader("Busiest routes")
    busiest = query(
        f"""
        SELECT rt, rtnm, COUNT(*) AS vehicle_count
        FROM vehicles_processed
        WHERE {where}
        GROUP BY rt, rtnm
        ORDER BY vehicle_count DESC
        LIMIT 10
        """,
        tuple(params),
    )
    if not busiest.empty:
        fig = px.bar(
            busiest.sort_values("vehicle_count"), x="vehicle_count", y="rtnm",
            orientation="h", labels={"vehicle_count": "Observations", "rtnm": ""},
            color="vehicle_count", color_continuous_scale="Blues",
        )
        fig.update_layout(coloraxis_showscale=False)
        st.plotly_chart(chart_layout(fig), use_container_width=True)

st.markdown("---")
st.subheader("Activity heatmap · hour × day of week")
heatmap = query(
    f"""
    SELECT hour_of_day, day_of_week, COUNT(*) AS activity
    FROM vehicles_processed
    WHERE {where}
    GROUP BY hour_of_day, day_of_week
    """,
    tuple(params),
)
if not heatmap.empty:
    pivot = heatmap.pivot(index="day_of_week", columns="hour_of_day", values="activity")
    pivot = pivot.reindex(index=range(7), columns=range(24), fill_value=0).fillna(0)
    fig = px.imshow(
        pivot, labels={"x": "Hour of day", "y": "Day of week", "color": "Vehicles"},
        x=list(range(24)), y=DAY_LABELS, color_continuous_scale="YlOrRd", aspect="auto",
    )
    st.plotly_chart(chart_layout(fig, 280), use_container_width=True)

st.markdown("---")
st.subheader("Route performance")
route_perf = query(
    f"""
    SELECT rt, rtnm, COUNT(*) AS observations,
           AVG(delay_minutes) AS avg_delay,
           SUM(CASE WHEN is_delayed = 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS pct_on_time,
           AVG(CASE WHEN is_peak = 1 THEN delay_minutes END) AS peak_delay,
           AVG(CASE WHEN is_peak = 0 THEN delay_minutes END) AS offpeak_delay
    FROM vehicles_processed
    WHERE {where}
    GROUP BY rt, rtnm
    HAVING COUNT(*) >= 10
    ORDER BY avg_delay DESC
    """,
    tuple(params),
)
if not route_perf.empty:
    col_a, col_b = st.columns(2)
    with col_a:
        fig = px.bar(
            route_perf, x="rtnm", y="avg_delay", color="avg_delay",
            color_continuous_scale="Reds", labels={"avg_delay": "Delay (min)", "rtnm": ""},
        )
        fig.update_layout(coloraxis_showscale=False, xaxis_tickangle=35)
        st.plotly_chart(chart_layout(fig, 340), use_container_width=True)
    with col_b:
        fig = px.bar(
            route_perf, x="rtnm", y="pct_on_time", color="pct_on_time",
            color_continuous_scale="Greens", labels={"pct_on_time": "On-time %", "rtnm": ""},
        )
        fig.update_layout(coloraxis_showscale=False, xaxis_tickangle=35)
        st.plotly_chart(chart_layout(fig, 340), use_container_width=True)

    comparison = route_perf[["rtnm", "peak_delay", "offpeak_delay"]].melt(
        id_vars="rtnm", var_name="period", value_name="delay"
    )
    comparison["period"] = comparison["period"].map(
        {"peak_delay": "Peak", "offpeak_delay": "Off-peak"}
    )
    fig = px.bar(
        comparison, x="rtnm", y="delay", color="period", barmode="group",
        labels={"delay": "Delay (min)", "rtnm": "", "period": ""},
        color_discrete_map={"Peak": "#f97316", "Off-peak": "#60a5fa"},
    )
    fig.update_layout(xaxis_tickangle=35)
    st.plotly_chart(chart_layout(fig, 320), use_container_width=True)
else:
    st.info("Not enough observations for route comparison.")

st.markdown("---")
st.subheader("Vehicle geography")
geo_data = query(
    f"""
    SELECT v.vid, v.rt, v.rtnm, v.lat, v.lon, v.delay_minutes, v.is_delayed
    FROM vehicles_processed v
    INNER JOIN (
        SELECT vid, MAX(recorded_at) AS latest
        FROM vehicles_processed
        WHERE {where}
        GROUP BY vid
    ) latest ON v.vid = latest.vid AND v.recorded_at = latest.latest
    WHERE v.lat BETWEEN 41.6 AND 42.1
      AND v.lon BETWEEN -88.0 AND -87.4
    """,
    tuple(params),
)
if geo_data.empty:
    st.info("No vehicle coordinates are available for this filter.")
else:
    map_args = dict(
        data_frame=geo_data, lat="lat", lon="lon", color="delay_minutes",
        hover_name="rtnm",
        hover_data={"vid": True, "delay_minutes": ":.1f", "lat": False, "lon": False},
        color_continuous_scale="RdYlGn_r", range_color=[0, 15], zoom=9.5,
        center={"lat": 41.878, "lon": -87.630}, height=480,
        labels={"delay_minutes": "Delay (min)"},
    )
    if hasattr(px, "scatter_map"):
        fig = px.scatter_map(**map_args, map_style="open-street-map")
    else:
        fig = px.scatter_mapbox(**map_args, mapbox_style="open-street-map")
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0))
    st.plotly_chart(fig, use_container_width=True)

st.markdown("---")
st.subheader("Route KPI detail")
detail = query(
    f"""
    SELECT rt AS "Route", rtnm AS "Name", COUNT(*) AS "Observations",
           ROUND(AVG(delay_minutes), 2) AS "Avg delay (min)",
           ROUND(MAX(delay_minutes), 2) AS "Max delay (min)",
           ROUND(SUM(CASE WHEN is_delayed = 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS "On-time %",
           ROUND(AVG(CASE WHEN is_peak = 1 THEN delay_minutes END), 2) AS "Peak delay (min)",
           ROUND(AVG(CASE WHEN is_peak = 0 THEN delay_minutes END), 2) AS "Off-peak delay (min)",
           ROUND(AVG(headway_minutes), 1) AS "Avg headway (min)"
    FROM vehicles_processed
    WHERE {where}
    GROUP BY rt, rtnm
    ORDER BY "Avg delay (min)" DESC
    """,
    tuple(params),
)
st.dataframe(
    detail, use_container_width=True, hide_index=True,
    column_config={
        "On-time %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f%%"),
    },
)
if not detail.empty:
    st.download_button(
        "Download route KPIs as CSV", detail.to_csv(index=False),
        file_name="cta_route_kpis.csv", mime="text/csv",
    )

st.markdown("---")
st.subheader("Recent service alerts")
alert_params: list[str] = []
alert_where = "1 = 1"
if selected_route:
    alert_where = "(',' || a.rt || ',') LIKE '%,' || ? || ',%'"
    alert_params.append(selected_route)
alerts = query(
    f"""
    SELECT a.rt, COALESCE(r.rtnm, a.rt) AS route_name,
           a.short_description, a.impact, a.severity_score, a.active_dt
    FROM raw_alerts a
    LEFT JOIN (
        SELECT rt, rtnm FROM raw_routes
        WHERE id IN (SELECT MAX(id) FROM raw_routes GROUP BY rt)
    ) r ON a.rt = r.rt
    WHERE {alert_where}
    ORDER BY a.id DESC
    LIMIT 20
    """,
    tuple(alert_params),
)
if alerts.empty:
    st.info("No service alerts for the selected filter.")
else:
    severity_icon = {1: "🟢", 2: "🟡", 3: "🔴"}
    for row in alerts.itertuples():
        icon = severity_icon.get(int(row.severity_score or 1), "⚪")
        date_label = str(row.active_dt)[:10] if row.active_dt else "N/A"
        st.markdown(f"{icon} **{row.route_name}** — {row.short_description}  \n*{row.impact}* · {date_label}")

st.markdown("---")
st.caption("CTA TransitPulse · Python · Pandas · SQLite · SQL · Streamlit · Plotly")
