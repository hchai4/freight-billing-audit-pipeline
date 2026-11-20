"""Readers for the reconciliation mart: live PostgreSQL or a bundled snapshot.

The hosted demo has no warehouse, so it serves a CSV exported from the real
dbt mart (see data/export_demo_snapshot.py) through the same column contract.
"""

from datetime import date
from pathlib import Path
from typing import Optional

import re

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
    -- Billed minus expected. Not the claim amount: a late-delivery refund or an
    -- unauthorized fee can leave this negative while money is still recoverable.
    m.total_variance_usd AS dollar_variance,
    -- What the carrier owes back, each dollar attributed once (see the mart).
    COALESCE(m.total_recoverable_usd, 0) AS recoverable_usd,
    m.anomaly_codes AS anomaly_classification,
    COALESCE(m.total_recoverable_usd, 0) > 0 AS is_eligible_for_dispute,
    CASE WHEN COALESCE(e.has_dispute_sent, FALSE) THEN 'DISPUTE_SENT'
         ELSE 'PENDING_REVIEW' END AS dispute_status,
    -- Evidence behind each reason code (rendered by src/agent/dispute_evidence.py)
    m.billable_weight_expected_lbs,
    m.dim_weight_lbs,
    m.billable_weight_basis,
    m.zone,
    m.zone_billed,
    m.weight_tier_min_lbs,
    m.weight_tier_max_lbs,
    m.service_level,
    m.service_level_billed,
    m.contract_base_rate_usd,
    m.billed_base_rate_usd,
    m.base_rate_variance_usd,
    m.expected_fuel_surcharge_usd,
    m.billed_fuel_surcharge_usd,
    m.fuel_surcharge_variance_usd,
    m.dest_address_type,
    m.is_dest_address_cass_validated,
    m.unauthorized_residential_fee_usd,
    m.unauthorized_address_correction_fee_usd,
    m.unauthorized_accessorial_usd,
    m.unauthorized_accessorial_codes,
    m.distinct_invoice_number_count,
    m.all_invoice_numbers,
    m.all_source_channels,
    m.duplicate_billed_amount_usd,
    m.guaranteed_delivery_timestamp,
    m.actual_delivery_timestamp,
    m.delivery_variance_hours,
    m.gsr_refund_usd,
    m.base_rate_recovery_usd,
    m.fuel_recovery_usd,
    m.accessorial_recovery_usd,
    m.duplicate_recovery_usd
FROM analytics_marts.fct_reconciliation_marts m
LEFT JOIN (
    -- Pre-aggregated so repeated audit rows per tracking_id cannot fan out the mart.
    SELECT tracking_id, bool_or(dispute_status = 'DISPUTE_SENT') AS has_dispute_sent
    FROM source_enterprise.audit_flagged_exceptions
    GROUP BY tracking_id
) e ON m.tracking_id = e.tracking_id
ORDER BY recoverable_usd DESC, tracking_id;
"""

# Timestamps that move with the scan date when the snapshot is rebased.
SNAPSHOT_TIME_COLUMNS = (
    "scan_timestamp",
    "guaranteed_delivery_timestamp",
    "actual_delivery_timestamp",
)


def select_columns(query: str = MART_QUERY) -> list:
    """Output column names of the mart query's top-level SELECT list.

    The hosted demo has no database, so its snapshot must carry every column
    the query returns; tests compare the two using this.
    """
    sql = re.sub(r"--[^\n]*", "", query)
    body = re.split(r"\bFROM\b", sql, maxsplit=1, flags=re.IGNORECASE)[0]
    body = re.sub(r"^\s*SELECT\b", "", body, flags=re.IGNORECASE)
    items, depth, current = [], 0, ""
    for char in body:
        depth += (char == "(") - (char == ")")
        if char == "," and depth == 0:
            items.append(current)
            current = ""
        else:
            current += char
    items.append(current)
    columns = []
    for item in (part.strip() for part in items):
        if not item:
            continue
        alias = re.search(r"\bAS\s+(\w+)\s*$", item, flags=re.IGNORECASE)
        columns.append(alias.group(1) if alias else item.split(".")[-1].strip())
    return columns


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
        parse_dates=[c for c in SNAPSHOT_TIME_COLUMNS],
    )
    df["anomaly_classification"] = df["anomaly_classification"].fillna("")
    df["is_eligible_for_dispute"] = df["is_eligible_for_dispute"].astype(bool)

    newest = df["scan_timestamp"].max()
    if pd.notna(newest):
        today = today or pd.Timestamp.now(tz="UTC").date()
        target = pd.Timestamp(today) - pd.Timedelta(days=DEMO_NEWEST_SCAN_AGE_DAYS)
        shift = target - newest.normalize()
        for column in SNAPSHOT_TIME_COLUMNS:
            df[column] += shift
    return df
