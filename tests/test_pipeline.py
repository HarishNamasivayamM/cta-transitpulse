from pathlib import Path

import sqlite3

from src.ingestion import CTAIngestion
from src.processing import CTAProcessor, _is_peak


def build_demo_database(tmp_path: Path, days: int = 1) -> Path:
    db_path = tmp_path / "cta_test.db"
    ingestion = CTAIngestion(
        db_path=db_path,
        raw_dir=tmp_path / "raw",
        processed_dir=tmp_path / "processed",
    )
    try:
        ingestion.generate_sample_data(days=days)
    finally:
        ingestion.close()

    processor = CTAProcessor(db_path=db_path)
    try:
        processor.run()
    finally:
        processor.close()
    return db_path


def test_sample_ingestion_and_processing_are_end_to_end(tmp_path: Path) -> None:
    db_path = build_demo_database(tmp_path)

    with sqlite3.connect(db_path) as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "raw_vehicles",
                "raw_predictions",
                "vehicles_processed",
                "predictions_processed",
                "route_stats",
            )
        }

    assert counts["raw_vehicles"] > 0
    assert counts["raw_predictions"] > counts["raw_vehicles"]
    assert counts["vehicles_processed"] == counts["raw_vehicles"]
    assert counts["predictions_processed"] == counts["raw_predictions"]
    assert counts["route_stats"] > 0


def test_sample_reset_prevents_accidental_append(tmp_path: Path) -> None:
    db_path = tmp_path / "cta_test.db"
    ingestion = CTAIngestion(
        db_path=db_path,
        raw_dir=tmp_path / "raw",
        processed_dir=tmp_path / "processed",
    )
    try:
        ingestion.generate_sample_data(days=1)
        first_count = ingestion.conn.execute("SELECT COUNT(*) FROM raw_vehicles").fetchone()[0]
        ingestion.reset_database()
        ingestion.generate_sample_data(days=1)
        second_count = ingestion.conn.execute("SELECT COUNT(*) FROM raw_vehicles").fetchone()[0]
    finally:
        ingestion.close()

    assert first_count == second_count


def test_peak_definition_excludes_weekends() -> None:
    assert _is_peak(8, False) == 1
    assert _is_peak(17, False) == 1
    assert _is_peak(12, False) == 0
    assert _is_peak(8, True) == 0

