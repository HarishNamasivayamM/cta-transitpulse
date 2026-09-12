from pathlib import Path

from streamlit.testing.v1 import AppTest

from src.ingestion import CTAIngestion
from src.processing import CTAProcessor


def test_dashboard_smoke(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "cta_dashboard.db"
    ingestion = CTAIngestion(
        db_path=db_path,
        raw_dir=tmp_path / "raw",
        processed_dir=tmp_path / "processed",
    )
    try:
        ingestion.generate_sample_data(days=1)
    finally:
        ingestion.close()

    processor = CTAProcessor(db_path=db_path)
    try:
        processor.run()
    finally:
        processor.close()

    monkeypatch.setenv("CTA_DB_PATH", str(db_path))
    app = AppTest.from_file("src/dashboard.py", default_timeout=60)
    app.run()

    assert not app.exception
    assert len(app.metric) == 5
    assert len(app.dataframe) == 1

