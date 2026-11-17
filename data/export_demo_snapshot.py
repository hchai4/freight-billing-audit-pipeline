"""Exports the live reconciliation mart to the CSV the hosted demo serves.

Run after `dbt run` and the anomaly detector, against the Compose database:

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/logistics_db \
      python data/export_demo_snapshot.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402

from src.db.mart_reader import DEMO_SNAPSHOT_PATH, load_mart_from_db  # noqa: E402

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/logistics_db"
)

if __name__ == "__main__":
    df = load_mart_from_db(create_engine(DATABASE_URL))
    # Every visitor starts from a clean slate; disputes are tracked per session.
    df["dispute_status"] = "PENDING_REVIEW"
    DEMO_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(DEMO_SNAPSHOT_PATH, index=False)
    print(f"Wrote {len(df)} rows to {DEMO_SNAPSHOT_PATH}")
