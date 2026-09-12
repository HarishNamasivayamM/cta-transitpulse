# Data directory

The repository keeps the `raw/` and `processed/` directory structure, but generated API responses and SQLite databases are ignored by Git. Build a fresh deterministic demo dataset with:

```bash
python -m src.pipeline --sample --days 7 --reset
```

The dashboard reads `data/cta_data.db` by default.

