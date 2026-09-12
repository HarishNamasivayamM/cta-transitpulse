"""Run the complete CTA TransitPulse pipeline from one command.

Examples:
    python -m src.pipeline --sample --days 7 --reset
    python -m src.pipeline --once
    python -m src.pipeline --sample --db-path data/demo.db --reset
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .ingestion import CTAIngestion, DB_PATH, PROCESSED_DIR, RAW_DIR
from .processing import CTAProcessor


def run_pipeline(
    *,
    sample: bool = True,
    days: int = 7,
    reset: bool = False,
    once: bool = False,
    db_path: Path = DB_PATH,
) -> None:
    """Ingest data and rebuild all analytical tables."""
    if sample and once:
        raise ValueError("Choose either sample mode or live one-shot mode, not both")

    ingestion = CTAIngestion(
        db_path=db_path,
        raw_dir=db_path.parent / "raw" if db_path != DB_PATH else RAW_DIR,
        processed_dir=db_path.parent / "processed" if db_path != DB_PATH else PROCESSED_DIR,
    )
    try:
        if reset:
            ingestion.reset_database()
        if sample:
            ingestion.generate_sample_data(days=days)
        else:
            ingestion.run_once()
    finally:
        ingestion.close()

    processor = CTAProcessor(db_path=db_path)
    try:
        processor.run()
    finally:
        processor.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CTA TransitPulse pipeline")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--sample",
        action="store_true",
        help="Generate deterministic demo data (the default)",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="Fetch one live CTA Bus Tracker cycle using .env credentials",
    )
    parser.add_argument("--days", type=int, default=7, help="Demo history to generate")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop existing raw and processed tables before ingestion",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DB_PATH,
        help="SQLite database path (default: data/cta_data.db)",
    )
    args = parser.parse_args()

    run_pipeline(
        sample=not args.once,
        days=args.days,
        reset=args.reset,
        once=args.once,
        db_path=args.db_path,
    )
    print(f"Pipeline complete: {args.db_path}")


if __name__ == "__main__":
    main()

