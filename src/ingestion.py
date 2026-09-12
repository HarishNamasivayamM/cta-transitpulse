"""
CTA Transit Data Ingestion Module

Fetches real-time data from CTA Bus Tracker and Train Tracker APIs.
Falls back to realistic sample data generation when API keys are absent,
so the full dashboard can be demonstrated without any API setup.

Usage:
    python ingestion.py --sample          # Generate 7 days of sample data (no key needed)
    python ingestion.py --sample --days 14
    python ingestion.py --once            # Single live API ingestion cycle
    python ingestion.py                   # Continuous polling every 60 s
    python ingestion.py --interval 120    # Continuous polling every 120 s
"""

import argparse
import json
import logging
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "cta_data.db"
RAW_DIR = BASE_DIR / "data" / "raw"
PROCESSED_DIR = BASE_DIR / "data" / "processed"

# ---------------------------------------------------------------------------
# API configuration
# ---------------------------------------------------------------------------
CTA_BUS_KEY = os.getenv("CTA_BUS_API_KEY", "")
CTA_TRAIN_KEY = os.getenv("CTA_TRAIN_API_KEY", "")
BUS_API_BASE = "http://ctabustracker.com/bustime/api/v2"
TRAIN_API_BASE = "http://lapi.transitchicago.com/api/1.0"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "ingestion.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_routes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rt         TEXT NOT NULL,
    rtnm       TEXT,
    rtclr      TEXT,
    rtdd       TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_vehicles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    vid           TEXT,
    tmstmp        TEXT,
    lat           REAL,
    lon           REAL,
    hdg           INTEGER,
    pid           INTEGER,
    rt            TEXT,
    des           TEXT,
    pdist         INTEGER,
    dly           INTEGER,           -- 1=delayed flag from API (boolean)
    delay_minutes REAL DEFAULT 0,   -- estimated actual delay in minutes
    tatripid      TEXT,
    tablockid     TEXT,
    zone          TEXT,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_predictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tmstmp      TEXT,
    typ         TEXT,
    stpnm       TEXT,
    stpid       TEXT,
    vid         TEXT,
    dstp        INTEGER,
    rt          TEXT,
    rtdd        TEXT,
    rtdir       TEXT,
    des         TEXT,
    prdtm       TEXT,
    tablockid   TEXT,
    tatripid    TEXT,
    dly         INTEGER,
    prdctdn     INTEGER,            -- minutes until arrival
    zone        TEXT,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_alerts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id          TEXT,
    short_description TEXT,
    full_description  TEXT,
    severity_score    INTEGER,
    impact            TEXT,
    active_dt         TEXT,
    rt                TEXT,
    fetched_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vehicles_processed (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vid             TEXT,
    rt              TEXT,
    rtnm            TEXT,
    lat             REAL,
    lon             REAL,
    is_delayed      INTEGER,
    delay_minutes   REAL,
    hour_of_day     INTEGER,
    day_of_week     INTEGER,        -- 0=Monday, 6=Sunday
    is_peak         INTEGER,        -- 1 during AM/PM rush on weekdays
    is_weekend      INTEGER,
    recorded_at     TEXT,
    headway_minutes REAL
);

CREATE TABLE IF NOT EXISTS predictions_processed (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    rt             TEXT,
    rtnm           TEXT,
    stpnm          TEXT,
    stpid          TEXT,
    vid            TEXT,
    rtdir          TEXT,
    des            TEXT,
    minutes_to_arr INTEGER,
    is_delayed     INTEGER,
    hour_of_day    INTEGER,
    day_of_week    INTEGER,
    is_peak        INTEGER,
    is_weekend     INTEGER,
    predicted_at   TEXT
);

CREATE TABLE IF NOT EXISTS route_stats (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    rt                  TEXT,
    rtnm                TEXT,
    stat_date           TEXT,
    hour_of_day         INTEGER,
    day_of_week         INTEGER,
    avg_delay_minutes   REAL,
    pct_on_time         REAL,
    vehicle_count       INTEGER,
    prediction_count    INTEGER,
    avg_headway_minutes REAL,
    is_peak             INTEGER
);
"""

# ---------------------------------------------------------------------------
# Static data: real Chicago CTA bus routes
# ---------------------------------------------------------------------------
ROUTES = [
    {"rt": "8",   "rtnm": "Halsted",             "rtclr": "#e27ea6"},
    {"rt": "22",  "rtnm": "Clark",               "rtclr": "#00a1de"},
    {"rt": "36",  "rtnm": "Broadway",            "rtclr": "#009b3a"},
    {"rt": "49",  "rtnm": "Western",             "rtclr": "#f9461c"},
    {"rt": "52",  "rtnm": "Kedzie",              "rtclr": "#00a1de"},
    {"rt": "53",  "rtnm": "Pulaski",             "rtclr": "#009b3a"},
    {"rt": "66",  "rtnm": "Chicago",             "rtclr": "#e27ea6"},
    {"rt": "72",  "rtnm": "North",               "rtclr": "#f9461c"},
    {"rt": "77",  "rtnm": "Belmont",             "rtclr": "#009b3a"},
    {"rt": "79",  "rtnm": "79th",                "rtclr": "#e27ea6"},
    {"rt": "82",  "rtnm": "Kimball-Homan",       "rtclr": "#00a1de"},
    {"rt": "147", "rtnm": "Outer Drive Express", "rtclr": "#f9461c"},
    {"rt": "151", "rtnm": "Sheridan",            "rtclr": "#009b3a"},
    {"rt": "155", "rtnm": "Devon",               "rtclr": "#e27ea6"},
]

# Per-route delay profile (minutes) modeled on real Chicago congestion patterns
ROUTE_DELAY_PROFILE = {
    "8":   {"base": 3.0, "peak_add": 4.0},
    "22":  {"base": 3.5, "peak_add": 4.5},
    "36":  {"base": 2.5, "peak_add": 3.0},
    "49":  {"base": 4.0, "peak_add": 6.0},   # Western is chronically delayed
    "52":  {"base": 3.0, "peak_add": 4.0},
    "53":  {"base": 3.5, "peak_add": 5.0},
    "66":  {"base": 4.0, "peak_add": 5.5},
    "72":  {"base": 3.0, "peak_add": 4.0},
    "77":  {"base": 3.5, "peak_add": 4.0},
    "79":  {"base": 5.5, "peak_add": 7.0},   # 79th is the most delayed
    "82":  {"base": 3.0, "peak_add": 3.5},
    "147": {"base": 1.5, "peak_add": 2.0},   # Express — usually fast
    "151": {"base": 2.0, "peak_add": 2.5},
    "155": {"base": 3.0, "peak_add": 3.5},
}

# Approximate geographic center [lat, lon] for each route corridor
ROUTE_COORDS = {
    "8":   (41.8875, -87.6448),
    "22":  (41.9300, -87.6316),
    "36":  (41.9500, -87.6473),
    "49":  (41.8800, -87.6886),
    "52":  (41.8400, -87.7068),
    "53":  (41.8600, -87.7260),
    "66":  (41.8964, -87.7200),
    "72":  (41.9095, -87.7000),
    "77":  (41.9395, -87.7100),
    "79":  (41.7513, -87.7000),
    "82":  (41.8700, -87.7100),
    "147": (41.9000, -87.6300),
    "151": (41.9300, -87.6200),
    "155": (41.9985, -87.6800),
}

STOP_NAMES = [
    "State & Madison", "Clark & Division", "Western & Belmont",
    "Halsted & North", "Chicago & Pulaski", "Broadway & Sheridan",
    "79th & Stony Island", "Devon & Western", "North & Kedzie",
    "Belmont & Cicero", "Chicago & Austin", "Halsted & 63rd",
    "Western & 95th", "Clark & Howard", "Sheridan & Diversey",
]


# ---------------------------------------------------------------------------
# Main ingestion class
# ---------------------------------------------------------------------------

class CTAIngestion:
    def __init__(
        self,
        db_path: Path = DB_PATH,
        raw_dir: Path = RAW_DIR,
        processed_dir: Path = PROCESSED_DIR,
    ):
        """Create an ingestion client with injectable paths for testing."""
        self.db_path = Path(db_path)
        self.raw_dir = Path(raw_dir)
        self.processed_dir = Path(processed_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()
        log.info("Database ready: %s", self.db_path)

    def reset_database(self) -> None:
        """Clear raw and processed tables for a fresh reproducible run."""
        tables = (
            "raw_routes",
            "raw_vehicles",
            "raw_predictions",
            "raw_alerts",
            "vehicles_processed",
            "predictions_processed",
            "route_stats",
        )
        for table in tables:
            self.conn.execute(f"DROP TABLE IF EXISTS {table}")
        self.conn.commit()
        self._init_db()
        log.info("Reset database: %s", self.db_path)

    # ------------------------------------------------------------------
    # Live API helpers
    # ------------------------------------------------------------------

    def _bus_get(self, endpoint: str, params: dict) -> Optional[dict]:
        if not CTA_BUS_KEY:
            log.warning("CTA_BUS_API_KEY not set — skipping live call to %s", endpoint)
            return None
        url = f"{BUS_API_BASE}/{endpoint}"
        params = {**params, "key": CTA_BUS_KEY, "format": "json"}
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
            raw_file = self.raw_dir / f"{endpoint}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            raw_file.write_text(json.dumps(payload, indent=2))
            return payload.get("bustime-response", {})
        except requests.RequestException as exc:
            log.error("Bus API error (%s): %s", endpoint, exc)
            return None

    def _train_get(self, endpoint: str, params: dict) -> Optional[dict]:
        if not CTA_TRAIN_KEY:
            log.warning("CTA_TRAIN_API_KEY not set — skipping live call to %s", endpoint)
            return None
        url = f"{TRAIN_API_BASE}/{endpoint}"
        params = {**params, "key": CTA_TRAIN_KEY, "outputType": "JSON"}
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
            raw_file = self.raw_dir / f"train_{endpoint}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            raw_file.write_text(json.dumps(payload, indent=2))
            return payload.get("ctatt", {})
        except requests.RequestException as exc:
            log.error("Train API error (%s): %s", endpoint, exc)
            return None

    # ------------------------------------------------------------------
    # Live API ingestion methods
    # ------------------------------------------------------------------

    def fetch_and_store_routes(self) -> list:
        data = self._bus_get("getroutes", {})
        if not data:
            return []
        routes = data.get("routes", [])
        now = datetime.now().isoformat()
        rows = [(r["rt"], r.get("rtnm"), r.get("rtclr"), r.get("rtdd"), now) for r in routes]
        self.conn.executemany(
            "INSERT INTO raw_routes (rt, rtnm, rtclr, rtdd, fetched_at) VALUES (?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        log.info("Stored %d routes", len(rows))
        return routes

    def fetch_and_store_vehicles(self, route_ids: list) -> int:
        total = 0
        now = datetime.now().isoformat()
        for rt in route_ids:
            data = self._bus_get("getvehicles", {"rt": rt})
            if not data:
                continue
            vehicles = data.get("vehicle", [])
            if isinstance(vehicles, dict):
                vehicles = [vehicles]
            rows = [
                (
                    v.get("vid"), v.get("tmstmp"),
                    float(v.get("lat", 0)), float(v.get("lon", 0)),
                    int(v.get("hdg", 0)), v.get("pid"), v.get("rt"),
                    v.get("des"), v.get("pdist"),
                    1 if v.get("dly") else 0,
                    0.0,  # delay_minutes computed during processing
                    v.get("tatripid"), v.get("tablockid"), v.get("zone"), now,
                )
                for v in vehicles
            ]
            if rows:
                self.conn.executemany(
                    """INSERT INTO raw_vehicles
                       (vid,tmstmp,lat,lon,hdg,pid,rt,des,pdist,dly,delay_minutes,
                        tatripid,tablockid,zone,fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    rows,
                )
                self.conn.commit()
                total += len(rows)
            time.sleep(0.4)
        log.info("Stored %d vehicle records", total)
        return total

    def fetch_and_store_predictions(self, route_ids: list) -> int:
        total = 0
        now = datetime.now().isoformat()
        for rt in route_ids:
            data = self._bus_get("getpredictions", {"rt": rt, "top": 50})
            if not data:
                continue
            preds = data.get("prd", [])
            if isinstance(preds, dict):
                preds = [preds]
            rows = [
                (
                    p.get("tmstmp"), p.get("typ"), p.get("stpnm"),
                    p.get("stpid"), p.get("vid"), p.get("dstp"),
                    p.get("rt"), p.get("rtdd"), p.get("rtdir"),
                    p.get("des"), p.get("prdtm"), p.get("tablockid"),
                    p.get("tatripid"), 1 if p.get("dly") else 0,
                    int(p.get("prdctdn", 0)), p.get("zone"), now,
                )
                for p in preds
            ]
            if rows:
                self.conn.executemany(
                    """INSERT INTO raw_predictions
                       (tmstmp,typ,stpnm,stpid,vid,dstp,rt,rtdd,rtdir,des,
                        prdtm,tablockid,tatripid,dly,prdctdn,zone,fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    rows,
                )
                self.conn.commit()
                total += len(rows)
            time.sleep(0.4)
        log.info("Stored %d prediction records", total)
        return total

    def fetch_and_store_alerts(self, route_ids: list) -> int:
        data = self._bus_get("getservicebulletins", {"rt": ",".join(route_ids[:10])})
        if not data:
            return 0
        bulletins = data.get("sb", [])
        if isinstance(bulletins, dict):
            bulletins = [bulletins]
        now = datetime.now().isoformat()
        rows = []
        for b in bulletins:
            srvc = b.get("srvc", [])
            if not isinstance(srvc, list):
                srvc = [srvc]
            rts = ",".join(str(s.get("rt", "")) for s in srvc)
            rows.append((
                b.get("nm"), b.get("sbj"), b.get("dsc"),
                b.get("prty"), b.get("brf"), b.get("beg"), rts, now,
            ))
        if rows:
            self.conn.executemany(
                """INSERT INTO raw_alerts
                   (alert_id,short_description,full_description,
                    severity_score,impact,active_dt,rt,fetched_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )
            self.conn.commit()
        log.info("Stored %d service alerts", len(rows))
        return len(rows)

    # ------------------------------------------------------------------
    # Sample data generation — no API key required
    # ------------------------------------------------------------------

    def generate_sample_data(self, days: int = 7) -> None:
        """
        Generates realistic CTA transit data for the past `days` days.

        Models:
        - Peak/off-peak ridership patterns (Mon-Fri 7-9 AM, 4-6 PM)
        - Per-route delay profiles (Western and 79th worst; express routes best)
        - Weekend ridership discounts (~40% fewer vehicles)
        - Gaussian delay noise with route-specific means
        - Realistic Chicago lat/lon spread per corridor
        """
        if days < 1:
            raise ValueError("days must be at least 1")

        log.info("Generating %d days of sample data…", days)
        random.seed(42)

        now = datetime.now().replace(second=0, microsecond=0)
        start = now - timedelta(days=days)

        # Seed routes table
        route_rows = [
            (r["rt"], r["rtnm"], r["rtclr"], r["rt"], start.isoformat())
            for r in ROUTES
        ]
        self.conn.executemany(
            "INSERT OR IGNORE INTO raw_routes (rt,rtnm,rtclr,rtdd,fetched_at) VALUES (?,?,?,?,?)",
            route_rows,
        )

        vehicle_rows, prediction_rows, alert_rows = [], [], []

        for day_offset in range(days):
            day_dt = start + timedelta(days=day_offset)
            is_weekend = day_dt.weekday() >= 5

            for hour in range(24):
                ts_dt = day_dt.replace(hour=hour, minute=0, second=0, microsecond=0)
                is_peak = (not is_weekend) and (hour in range(7, 10) or hour in range(16, 19))

                for route in ROUTES:
                    rt = route["rt"]
                    profile = ROUTE_DELAY_PROFILE[rt]
                    base_lat, base_lon = ROUTE_COORDS[rt]

                    # Vehicle count varies by time of day and day type
                    if hour < 5 or hour >= 23:
                        n_vehicles = random.randint(1, 3)
                    elif is_weekend:
                        n_vehicles = random.randint(3, 8)
                    elif is_peak:
                        n_vehicles = random.randint(10, 20)
                    else:
                        n_vehicles = random.randint(4, 12)

                    for v_idx in range(n_vehicles):
                        vid = f"{rt}-{v_idx:03d}"
                        lat = round(base_lat + random.uniform(-0.045, 0.045), 5)
                        lon = round(base_lon + random.uniform(-0.055, 0.055), 5)
                        minute = random.randint(0, 59)
                        tmstmp = ts_dt.replace(minute=minute).strftime("%Y%m%d %H:%M")

                        # Delay calculation
                        delay_mean = (
                            (profile["base"] + profile["peak_add"]) if is_peak
                            else profile["base"]
                        ) * (0.65 if is_weekend else 1.0)
                        delay_minutes = round(max(0.0, random.gauss(delay_mean, delay_mean * 0.55)), 2)
                        is_delayed = 1 if delay_minutes > 3.0 else 0

                        vehicle_rows.append((
                            vid, tmstmp, lat, lon,
                            random.choice([0, 90, 180, 270]),
                            random.randint(1000, 9999), rt,
                            f"{route['rtnm']} Terminal",
                            random.randint(500, 25000),
                            is_delayed, delay_minutes,
                            f"T{random.randint(100000, 999999)}",
                            f"{rt}-B{random.randint(100, 999)}",
                            "", ts_dt.isoformat(),
                        ))

                        # 3-5 stop predictions per vehicle
                        for stop_idx in range(random.randint(3, 5)):
                            stpid = f"{rt}-STP-{stop_idx:02d}"
                            stpnm = random.choice(STOP_NAMES)
                            prd_min = random.randint(2, 28)
                            prediction_rows.append((
                                tmstmp, "A", stpnm, stpid, vid,
                                random.randint(200, 8000),
                                rt, rt,
                                random.choice(["Northbound", "Southbound", "Eastbound", "Westbound"]),
                                f"{route['rtnm']} Terminal",
                                ts_dt.replace(minute=(minute + prd_min) % 60).strftime("%Y%m%d %H:%M"),
                                f"{rt}-B{random.randint(100, 999)}",
                                f"T{random.randint(100000, 999999)}",
                                is_delayed, prd_min, "", ts_dt.isoformat(),
                            ))

            # 2-4 service alerts per day
            for a_idx in range(random.randint(2, 4)):
                affected = random.choice(ROUTES)
                severity = random.choice([1, 2, 3])
                alert_rows.append((
                    f"ALT-{day_offset:02d}-{a_idx}",
                    f"Route {affected['rtnm']} — service advisory",
                    (
                        f"Due to {'construction' if random.random() > 0.5 else 'traffic'}, "
                        f"Route {affected['rtnm']} ({affected['rt']}) may experience "
                        f"{'significant' if severity == 3 else 'minor'} delays."
                    ),
                    severity,
                    ["Minor delay", "Major delay", "Reroute", "Suspended"][severity - 1],
                    day_dt.isoformat(),
                    affected["rt"],
                    day_dt.isoformat(),
                ))

        # Bulk inserts
        self.conn.executemany(
            """INSERT INTO raw_vehicles
               (vid,tmstmp,lat,lon,hdg,pid,rt,des,pdist,dly,delay_minutes,
                tatripid,tablockid,zone,fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            vehicle_rows,
        )
        self.conn.executemany(
            """INSERT INTO raw_predictions
               (tmstmp,typ,stpnm,stpid,vid,dstp,rt,rtdd,rtdir,des,
                prdtm,tablockid,tatripid,dly,prdctdn,zone,fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            prediction_rows,
        )
        self.conn.executemany(
            """INSERT INTO raw_alerts
               (alert_id,short_description,full_description,
                severity_score,impact,active_dt,rt,fetched_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            alert_rows,
        )
        self.conn.commit()
        log.info(
            "Sample data generated: %d vehicles | %d predictions | %d alerts",
            len(vehicle_rows), len(prediction_rows), len(alert_rows),
        )

    # ------------------------------------------------------------------
    # Run modes
    # ------------------------------------------------------------------

    def run_once(self) -> None:
        if not CTA_BUS_KEY:
            log.error(
                "CTA_BUS_API_KEY is not set. "
                "Add it to your .env file, or use --sample for demo data."
            )
            return
        routes = self.fetch_and_store_routes() or ROUTES
        route_ids = [r["rt"] for r in routes]
        self.fetch_and_store_vehicles(route_ids)
        self.fetch_and_store_predictions(route_ids)
        self.fetch_and_store_alerts(route_ids)
        log.info("Ingestion cycle complete.")

    def run_continuous(self, interval: int = 60) -> None:
        log.info("Starting continuous ingestion (interval=%ds). Ctrl-C to stop.", interval)
        while True:
            try:
                self.run_once()
            except Exception:
                log.exception("Ingestion cycle failed — will retry next interval")
            log.info("Sleeping %d seconds…", interval)
            time.sleep(interval)

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="CTA Transit Data Ingestion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sample", action="store_true",
        help="Generate realistic sample data (no API key required)",
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Days of sample data to generate (default: 7)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one live API ingestion cycle and exit",
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Polling interval in seconds for continuous mode (default: 60)",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Clear the database before generating sample data",
    )
    args = parser.parse_args()

    pipeline = CTAIngestion()
    try:
        if args.sample:
            if args.reset:
                pipeline.reset_database()
            pipeline.generate_sample_data(days=args.days)
        elif args.once:
            pipeline.run_once()
        else:
            pipeline.run_continuous(interval=args.interval)
    finally:
        pipeline.close()


if __name__ == "__main__":
    main()
