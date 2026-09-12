"""
CTA Transit Data Processing Module

Reads raw data from SQLite, cleans it, engineers analytical features,
and writes to the processed tables consumed by the dashboard and EDA notebook.

Usage:
    python processing.py
"""

import logging
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "cta_data.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Fallback route name map (used if raw_routes is empty)
ROUTE_NAMES = {
    "8": "Halsted", "22": "Clark", "36": "Broadway",
    "49": "Western", "52": "Kedzie", "53": "Pulaski",
    "66": "Chicago", "72": "North", "77": "Belmont",
    "79": "79th", "82": "Kimball-Homan", "147": "Outer Drive Express",
    "151": "Sheridan", "155": "Devon",
}

# Per-route average delay for filling in live API records (which only supply boolean dly)
ROUTE_AVG_DELAY = {
    "8": 5.0, "22": 5.8, "36": 4.0, "49": 7.0, "52": 5.0,
    "53": 6.0, "66": 6.8, "72": 5.0, "77": 5.5, "79": 9.0,
    "82": 4.5, "147": 2.5, "151": 3.0, "155": 4.5,
}

PEAK_HOURS = set(range(7, 10)) | set(range(16, 19))


def _is_peak(hour: int, is_weekend: bool) -> int:
    return int((not is_weekend) and (hour in PEAK_HOURS))


def _parse_cta_timestamp(values: pd.Series) -> pd.Series:
    """Parse CTA's compact timestamps and ISO timestamps used by the demo."""
    parsed = pd.to_datetime(values, format="%Y%m%d %H:%M", errors="coerce")
    missing = parsed.isna()
    if missing.any():
        parsed.loc[missing] = pd.to_datetime(values.loc[missing], errors="coerce")
    return parsed


class CTAProcessor:
    def __init__(self, db_path: Path = DB_PATH):
        if not db_path.exists():
            raise FileNotFoundError(
                f"Database not found at {db_path}. Run the ingestion step first."
            )
        self.conn = sqlite3.connect(str(db_path))

    # ------------------------------------------------------------------
    # Vehicles
    # ------------------------------------------------------------------

    def process_vehicles(self) -> int:
        log.info("Processing raw_vehicles…")

        df = pd.read_sql_query(
            """
            SELECT v.*,
                   COALESCE(r.rtnm, 'Unknown') AS rtnm
            FROM raw_vehicles v
            LEFT JOIN (
                SELECT rt, rtnm
                FROM raw_routes
                WHERE id IN (SELECT MAX(id) FROM raw_routes GROUP BY rt)
            ) r ON v.rt = r.rt
            """,
            self.conn,
        )

        if df.empty:
            log.warning("raw_vehicles is empty — nothing to process.")
            return 0

        # Parse timestamp (sample format: "YYYYMMDD HH:MM"; live format: ISO string)
        df["recorded_at"] = _parse_cta_timestamp(df["tmstmp"])
        fallback_mask = df["recorded_at"].isna()
        if fallback_mask.any():
            df.loc[fallback_mask, "recorded_at"] = pd.to_datetime(
                df.loc[fallback_mask, "fetched_at"], errors="coerce"
            )
        df = df.dropna(subset=["recorded_at"])

        # Temporal features
        df["hour_of_day"] = df["recorded_at"].dt.hour
        df["day_of_week"] = df["recorded_at"].dt.dayofweek   # 0=Monday
        df["is_weekend"] = (df["day_of_week"] >= 5).astype(int)
        df["is_peak"] = df.apply(
            lambda r: _is_peak(r["hour_of_day"], bool(r["is_weekend"])), axis=1
        )

        # Fill delay_minutes for live API records (which only have boolean dly).
        df["delay_minutes"] = pd.to_numeric(df["delay_minutes"], errors="coerce").fillna(0)
        df["dly"] = pd.to_numeric(df["dly"], errors="coerce").fillna(0).astype(int)
        no_delay_info = (df["delay_minutes"] == 0) & (df["dly"] == 1)
        if no_delay_info.any():
            estimates = (
                df.loc[no_delay_info, "rt"]
                .map(lambda rt: ROUTE_AVG_DELAY.get(str(rt), 5.0) + np.random.normal(0, 1.5))
                .clip(lower=0.5)
                .astype(float)
            )
            df.loc[no_delay_info, "delay_minutes"] = estimates

        # Headway: time gap between consecutive vehicles on the same route
        df = df.sort_values(["rt", "recorded_at"])
        df["headway_minutes"] = (
            df.groupby("rt")["recorded_at"]
            .diff()
            .dt.total_seconds()
            .div(60)
            .clip(0, 90)
        )

        # Fill missing route names from local map
        missing_name = df["rtnm"] == "Unknown"
        df.loc[missing_name, "rtnm"] = (
            df.loc[missing_name, "rt"].astype(str).map(ROUTE_NAMES).fillna("Unknown")
        )

        # De-duplicate (same vehicle same timestamp same route)
        df = df.drop_duplicates(subset=["vid", "tmstmp", "rt"])

        out = df[[
            "vid", "rt", "rtnm", "lat", "lon", "dly", "delay_minutes",
            "hour_of_day", "day_of_week", "is_peak", "is_weekend",
            "recorded_at", "headway_minutes",
        ]].copy()
        out.rename(columns={"dly": "is_delayed"}, inplace=True)
        out["recorded_at"] = out["recorded_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
        out["delay_minutes"] = out["delay_minutes"].round(2)
        out["headway_minutes"] = out["headway_minutes"].round(2)

        out.to_sql("vehicles_processed", self.conn, if_exists="replace", index=False)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vehicles_route_time "
            "ON vehicles_processed (rt, recorded_at)"
        )
        self.conn.commit()
        log.info("vehicles_processed: %d rows written.", len(out))
        return len(out)

    # ------------------------------------------------------------------
    # Predictions
    # ------------------------------------------------------------------

    def process_predictions(self) -> int:
        log.info("Processing raw_predictions…")

        df = pd.read_sql_query(
            """
            SELECT p.*,
                   COALESCE(r.rtnm, p.rt) AS rtnm
            FROM raw_predictions p
            LEFT JOIN (
                SELECT rt, rtnm FROM raw_routes
                WHERE id IN (SELECT MAX(id) FROM raw_routes GROUP BY rt)
            ) r ON p.rt = r.rt
            """,
            self.conn,
        )

        if df.empty:
            log.warning("raw_predictions is empty — nothing to process.")
            return 0

        df["predicted_at"] = _parse_cta_timestamp(df["tmstmp"])
        fallback = df["predicted_at"].isna()
        if fallback.any():
            df.loc[fallback, "predicted_at"] = pd.to_datetime(
                df.loc[fallback, "fetched_at"], errors="coerce"
            )
        df = df.dropna(subset=["predicted_at"])

        df["hour_of_day"] = df["predicted_at"].dt.hour
        df["day_of_week"] = df["predicted_at"].dt.dayofweek
        df["is_weekend"] = (df["day_of_week"] >= 5).astype(int)
        df["is_peak"] = df.apply(
            lambda r: _is_peak(r["hour_of_day"], bool(r["is_weekend"])), axis=1
        )
        df["minutes_to_arr"] = (
            pd.to_numeric(df["prdctdn"], errors="coerce")
            .fillna(0)
            .clip(lower=0)
            .astype(int)
        )
        df["dly"] = pd.to_numeric(df["dly"], errors="coerce").fillna(0).astype(int)

        df = df.drop_duplicates(subset=["vid", "stpid", "tmstmp"])

        out = df[[
            "rt", "rtnm", "stpnm", "stpid", "vid", "rtdir", "des",
            "minutes_to_arr", "dly", "hour_of_day", "day_of_week",
            "is_peak", "is_weekend", "predicted_at",
        ]].copy()
        out.rename(columns={"dly": "is_delayed"}, inplace=True)
        out["predicted_at"] = out["predicted_at"].dt.strftime("%Y-%m-%d %H:%M:%S")

        out.to_sql("predictions_processed", self.conn, if_exists="replace", index=False)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_predictions_route_time "
            "ON predictions_processed (rt, predicted_at)"
        )
        self.conn.commit()
        log.info("predictions_processed: %d rows written.", len(out))
        return len(out)

    # ------------------------------------------------------------------
    # Route-level aggregated statistics
    # ------------------------------------------------------------------

    def compute_route_stats(self) -> int:
        log.info("Computing route_stats…")

        vdf = pd.read_sql_query(
            """
            SELECT rt, rtnm, hour_of_day, day_of_week, is_peak,
                   is_delayed, delay_minutes, headway_minutes, recorded_at
            FROM vehicles_processed
            """,
            self.conn,
        )

        if vdf.empty:
            log.warning("vehicles_processed is empty — no route stats to compute.")
            return 0

        vdf["stat_date"] = pd.to_datetime(vdf["recorded_at"]).dt.date.astype(str)

        stats = (
            vdf.groupby(["rt", "rtnm", "stat_date", "hour_of_day", "day_of_week", "is_peak"])
            .agg(
                avg_delay_minutes=("delay_minutes", "mean"),
                pct_on_time=("is_delayed", lambda x: (1 - x.mean()) * 100),
                vehicle_count=("rt", "count"),
                avg_headway_minutes=("headway_minutes", lambda x: x[x > 0].mean()),
            )
            .reset_index()
        )

        # Merge prediction counts
        try:
            pdf = pd.read_sql_query(
                """
                SELECT rt, date(predicted_at) AS stat_date,
                       hour_of_day, COUNT(*) AS prediction_count
                FROM predictions_processed
                GROUP BY rt, stat_date, hour_of_day
                """,
                self.conn,
            )
            if not pdf.empty:
                stats = stats.merge(pdf, on=["rt", "stat_date", "hour_of_day"], how="left")
                stats["prediction_count"] = stats["prediction_count"].fillna(0).astype(int)
            else:
                stats["prediction_count"] = 0
        except Exception:
            stats["prediction_count"] = 0

        stats["avg_delay_minutes"] = stats["avg_delay_minutes"].round(3)
        stats["pct_on_time"] = stats["pct_on_time"].round(2)
        stats["avg_headway_minutes"] = stats["avg_headway_minutes"].round(3)

        stats.to_sql("route_stats", self.conn, if_exists="replace", index=False)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_route_stats_route_date "
            "ON route_stats (rt, stat_date)"
        )
        self.conn.commit()
        log.info("route_stats: %d rows written.", len(stats))
        return len(stats)

    # ------------------------------------------------------------------
    # Pipeline entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        v = self.process_vehicles()
        p = self.process_predictions()
        s = self.compute_route_stats()
        log.info(
            "Processing complete — vehicles: %d | predictions: %d | route stats: %d",
            v, p, s,
        )

    def close(self) -> None:
        self.conn.close()


if __name__ == "__main__":
    proc = CTAProcessor()
    try:
        proc.run()
    finally:
        proc.close()
