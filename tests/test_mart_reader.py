from datetime import date

import pandas as pd

from src.db.mart_reader import (
    DEMO_NEWEST_SCAN_AGE_DAYS,
    DEMO_SNAPSHOT_PATH,
    MART_QUERY,
    load_mart_snapshot,
    select_columns,
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
    "recoverable_usd",
    "billable_weight_expected_lbs",
    "contract_base_rate_usd",
    "billed_base_rate_usd",
    "unauthorized_residential_fee_usd",
    "all_invoice_numbers",
    "guaranteed_delivery_timestamp",
    "actual_delivery_timestamp",
    "gsr_refund_usd",
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


def test_recoverable_amount_is_present_for_every_eligible_claim():
    df = load_mart_snapshot()
    eligible = df[df["is_eligible_for_dispute"]]
    assert (eligible["recoverable_usd"] > 0).all()
    # Billed-minus-expected can be negative while money is still owed back.
    assert (eligible["dollar_variance"] < 0).any()


def test_delivery_timestamps_move_with_the_scan_date():
    today = date(2030, 6, 15)
    base = load_mart_snapshot(today=today)
    later = load_mart_snapshot(today=date(2030, 6, 25))
    shift = later["scan_timestamp"] - base["scan_timestamp"]
    assert (shift == pd.Timedelta(days=10)).all()
    sla = base.dropna(subset=["guaranteed_delivery_timestamp"])
    shifted = later.loc[sla.index, "guaranteed_delivery_timestamp"]
    assert (
        shifted - sla["guaranteed_delivery_timestamp"] == pd.Timedelta(days=10)
    ).all()


def test_select_columns_parses_aliases_and_plain_columns():
    cols = select_columns(
        "SELECT m.a, m.b AS bee, COALESCE(m.c, 0) AS cee,\n"
        "  -- a comment, with a comma\n"
        "  CASE WHEN x THEN 1 ELSE 2 END AS dee\nFROM t m"
    )
    assert cols == ["a", "bee", "cee", "dee"]


def test_demo_snapshot_has_every_column_the_database_query_returns():
    # The hosted demo reads the snapshot, so a column added to the query but not
    # re-exported would break it. Re-run data/export_demo_snapshot.py to fix.
    queried = select_columns(MART_QUERY)
    assert len(queried) == len(set(queried))
    snapshot = load_mart_snapshot()
    missing = [column for column in queried if column not in snapshot.columns]
    assert not missing, f"snapshot is missing {missing}; re-export it"
