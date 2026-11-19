import pandas as pd

from src.agent.dispute_agent import DisputeRequest, build_user_prompt
from src.agent.dispute_evidence import (
    build_evidence,
    evidence_lines,
    recovery_breakdown,
    split_codes,
)

NAN = float("nan")


def row(**overrides):
    base = {
        "tracking_id": "1Z0001",
        "invoice_number": "INV-1",
        "carrier_name": "UPS",
        "anomaly_classification": "",
        "recoverable_usd": 0.0,
        "billed_total": 50.0,
        "expected_total_cost": 40.0,
        "actual_scale_weight": 4.2,
        "billed_weight": 18.5,
        "weight_variance_lbs": 14.3,
        "billable_weight_expected_lbs": 4.2,
        "billable_weight_basis": "ACTUAL",
        "dim_weight_lbs": 3.1,
    }
    base.update(overrides)
    return base


def codes(r):
    return [block.code for block in build_evidence(r)]


def text(r):
    return " ".join(evidence_lines(r))


def test_split_codes_handles_empty_and_multiple():
    assert split_codes("") == []
    assert split_codes(None) == []
    assert split_codes(NAN) == []
    assert split_codes("A | B") == ["A", "B"]


def test_weight_inflation_cites_billed_vs_expected_weight():
    r = row(anomaly_classification="WEIGHT_INFLATION")
    out = text(r)
    assert "18.5 lb" in out and "4.2 lb" in out
    assert "+14.3 lb" in out


def test_base_rate_cites_contract_rate_and_lookup():
    r = row(
        anomaly_classification="BASE_RATE_OVERCHARGE",
        billed_base_rate_usd=50.0,
        contract_base_rate_usd=20.0,
        base_rate_variance_usd=30.0,
        zone="5",
        weight_tier_min_lbs=4.0,
        weight_tier_max_lbs=5.0,
        zone_billed="8",
        service_level="GROUND",
        service_level_billed="GROUND",
    )
    out = text(r)
    assert "$50.00" in out and "$20.00" in out
    assert "zone 5, 4-5 lb tier" in out
    assert "6 lb tier" in text(
        row(
            anomaly_classification="BASE_RATE_OVERCHARGE",
            zone="4",
            weight_tier_min_lbs=6.0,
            weight_tier_max_lbs=6.0,
        )
    )
    assert "billed zone 8" in out
    assert "Carrier billed service" not in out


def test_residential_fee_cites_address_type():
    r = row(
        anomaly_classification="UNAUTHORIZED_RESIDENTIAL_FEE",
        unauthorized_residential_fee_usd=5.2,
        dest_address_type="COMMERCIAL",
    )
    out = text(r)
    assert "$5.20" in out and "COMMERCIAL" in out
    assert "weight" not in out.lower()


def test_address_correction_cites_cass_validation():
    r = row(
        anomaly_classification="UNAUTHORIZED_ADDRESS_CORRECTION_FEE",
        unauthorized_address_correction_fee_usd=19.0,
        is_dest_address_cass_validated=True,
    )
    assert "USPS CASS software: yes" in text(r)


def test_duplicate_billing_cites_invoices_and_channels():
    r = row(
        anomaly_classification="DUPLICATE_BILLING_COLLISION",
        distinct_invoice_number_count=2,
        all_invoice_numbers="EDI-1, PDF-9",
        all_source_channels="UPS_EDI, PORTAL_PDF_AI",
        duplicate_billed_amount_usd=14.78,
    )
    out = text(r)
    assert "2 invoice(s)" in out and "EDI-1, PDF-9" in out and "$14.78" in out


def test_sla_failure_cites_promised_vs_actual_delivery():
    r = row(
        anomaly_classification="SLA_DELIVERY_FAILURE_REFUND",
        service_level="NEXT_DAY_AIR",
        guaranteed_delivery_timestamp=pd.Timestamp("2026-02-02 10:30"),
        actual_delivery_timestamp=pd.Timestamp("2026-02-02 15:00"),
        delivery_variance_hours=4.5,
        gsr_refund_usd=18.31,
    )
    out = text(r)
    assert "2026-02-02 10:30" in out and "2026-02-02 15:00" in out
    assert "4.5 h late" in out and "$18.31" in out


def test_fuel_variance_gets_its_own_block_when_recovered():
    r = row(
        fuel_recovery_usd=3.0,
        billed_fuel_surcharge_usd=9.0,
        expected_fuel_surcharge_usd=6.0,
        fuel_surcharge_variance_usd=3.0,
    )
    assert codes(r) == ["FUEL_SURCHARGE_VARIANCE"]


def test_unflagged_row_has_no_evidence():
    assert build_evidence(row()) == []


def test_missing_values_do_not_crash_or_print_nan():
    r = row(
        anomaly_classification="BASE_RATE_OVERCHARGE | SLA_DELIVERY_FAILURE_REFUND",
        billed_base_rate_usd=NAN,
        contract_base_rate_usd=None,
        guaranteed_delivery_timestamp=pd.NaT,
        actual_delivery_timestamp=pd.NaT,
        delivery_variance_hours=NAN,
        gsr_refund_usd=NAN,
    )
    out = text(r)
    assert "nan" not in out.lower()
    assert "n/a" in out


def test_recovery_breakdown_lists_only_positive_parts():
    r = row(
        gsr_refund_usd=0.0,
        base_rate_recovery_usd=10.0,
        fuel_recovery_usd=NAN,
        accessorial_recovery_usd=5.2,
        duplicate_recovery_usd=0.0,
    )
    assert recovery_breakdown(r) == [("Base rate", 10.0), ("Unauthorized fees", 5.2)]


def test_letter_prompt_uses_evidence_and_claim_not_weights():
    r = row(
        anomaly_classification="UNAUTHORIZED_RESIDENTIAL_FEE",
        unauthorized_residential_fee_usd=5.2,
        dest_address_type="COMMERCIAL",
        recoverable_usd=5.2,
    )
    req = DisputeRequest(
        invoice_number="INV-1",
        carrier_name="UPS",
        tracking_id="1Z0001",
        billed_weight=18.5,
        actual_weight=4.2,
        billed_cost=18.98,
        expected_cost=29.19,
        dollar_variance=5.2,
        anomaly_codes=r["anomaly_classification"],
        evidence=evidence_lines(r),
    )
    prompt = build_user_prompt(req)
    assert "Refund requested: $5.20" in prompt
    assert "[UNAUTHORIZED_RESIDENTIAL_FEE]" in prompt
    assert "18.5" not in prompt  # no weight comparison for a fee dispute
    assert "18.98" not in prompt and "29.19" not in prompt  # totals can mislead


def test_letter_prompt_without_evidence_keeps_the_weight_brief():
    req = DisputeRequest(
        invoice_number="INV-1",
        carrier_name="UPS",
        tracking_id="1Z0001",
        billed_weight=18.5,
        actual_weight=4.2,
        billed_cost=53.5,
        expected_cost=24.2,
        dollar_variance=29.3,
    )
    prompt = build_user_prompt(req)
    assert "Carrier Billed Weight: 18.5 lbs" in prompt
    assert "Overcharge Refund Claim: $29.3" in prompt
