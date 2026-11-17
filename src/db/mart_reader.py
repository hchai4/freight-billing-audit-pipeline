"""Readers for the reconciliation mart: live PostgreSQL or a bundled snapshot.

The hosted demo has no warehouse, so it serves a CSV exported from the real
dbt mart (see data/export_demo_snapshot.py) through the same column contract.
"""

from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

DEMO_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "demo" / "reconciliation_mart.csv"
)

# The snapshot's scans are frozen in time, but dispute aging is measured against
# today. Rebasing the newest scan to this age keeps every claim inside the 30-day
# filing window with a mix of critical and active urgency.
DEMO_NEWEST_SCAN_AGE_DAYS = 22

MART_QUERY = """
SELECT
    m.tracking_id,
    m.package_id,
    m.primary_invoice_number AS invoice_number,
    m.carrier_code AS carrier_name,
    m.primary_source_channel AS source_channel,
    m.scan_timestamp,
    m.actual_scale_weight_lbs AS actual_scale_weight,
    m.billed_weight_lbs AS billed_weight,
    m.weight_variance_lbs,
    m.expected_total_cost_usd AS expected_total_cost,
    -- All-lines total, not billed_total_usd: only this reconciles as
    -- billed - expected = total_variance_usd on duplicate-billing rows.
    m.billed_total_all_lines_usd AS billed_total,
    m.total_variance_usd AS dollar_variance,
    m.anomaly_codes AS anomaly_classification,
    COALESCE(m.total_recoverable_usd, 0) > 0 AS is_eligible_for_dispute,
    CASE WHEN COALESCE(e.has_dispute_sent, FALSE) THEN 'DISPUTE_SENT'
         ELSE 'PENDING_REVIEW' END AS dispute_status
FROM analytics_marts.fct_reconciliation_marts m
LEFT JOIN (
    -- Pre-aggregated so repeated audit rows per tracking_id cannot fan out the mart.
    SELECT tracking_id, bool_or(dispute_status = 'DISPUTE_SENT') AS has_dispute_sent
    FROM source_enterprise.audit_flagged_exceptions
    GROUP BY tracking_id
) e ON m.tracking_id = e.tracking_id
ORDER BY dollar_variance DESC;
"""


def load_mart_from_db(engine) -> pd.DataFrame:
    """Queries fct_reconciliation_marts joined with exception audit statuses."""
    return pd.read_sql(MART_QUERY, con=engine)


def load_mart_snapshot(
    path: Path = DEMO_SNAPSHOT_PATH, today: Optional[date] = None
) -> pd.DataFrame:
    """Loads the bundled mart export with scan dates rebased to stay current."""
    df = pd.read_csv(
        path,
        dtype={"tracking_id": str, "package_id": str, "invoice_number": str},
        parse_dates=["scan_timestamp"],
    )
    df["anomaly_classification"] = df["anomaly_classification"].fillna("")
    df["is_eligible_for_dispute"] = df["is_eligible_for_dispute"].astype(bool)

    newest = df["scan_timestamp"].max()
    if pd.notna(newest):
        today = today or pd.Timestamp.now(tz="UTC").date()
        target = pd.Timestamp(today) - pd.Timedelta(days=DEMO_NEWEST_SCAN_AGE_DAYS)
        df["scan_timestamp"] += target - newest.normalize()
    return df
