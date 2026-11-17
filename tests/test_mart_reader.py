from datetime import date

import pandas as pd

from src.db.mart_reader import (
    DEMO_NEWEST_SCAN_AGE_DAYS,
    DEMO_SNAPSHOT_PATH,
    load_mart_snapshot,
)

APP_COLUMNS = {
    "tracking_id",
    "package_id",
    "invoice_number",
    "carrier_name",
    "source_channel",
    "scan_timestamp",
    "actual_scale_weight",
    "billed_weight",
    "weight_variance_lbs",
    "expected_total_cost",
    "billed_total",
    "dollar_variance",
    "anomaly_classification",
    "is_eligible_for_dispute",
    "dispute_status",
}


def test_bundled_snapshot_matches_the_app_contract():
    df = load_mart_snapshot()
    assert DEMO_SNAPSHOT_PATH.exists()
    assert APP_COLUMNS <= set(df.columns)
    assert len(df) > 0
    assert df["is_eligible_for_dispute"].dtype == bool
    assert df["is_eligible_for_dispute"].any()
    assert df["anomaly_classification"].notna().all()


def test_snapshot_dates_are_rebased_inside_the_filing_window():
    today = date(2030, 6, 15)
    df = load_mart_snapshot(today=today)
    age_days = (pd.Timestamp(today) - df["scan_timestamp"].dt.normalize()).dt.days
    assert age_days.min() == DEMO_NEWEST_SCAN_AGE_DAYS
    assert age_days.max() <= 30
