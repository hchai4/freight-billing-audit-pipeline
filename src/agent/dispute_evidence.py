"""Turns a reconciliation-mart row into the evidence behind each reason code.

The mart decides *why* a shipment was flagged and how many dollars are
recoverable. This module only renders that evidence in words so the review
screen and the dispute letter cite the same facts. It never recomputes a claim
amount: the amount is the mart's ``recoverable_usd``.
"""

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Tuple

TITLES = {
    "WEIGHT_INFLATION": "Weight inflation",
    "BASE_RATE_OVERCHARGE": "Base rate above contract",
    "UNAUTHORIZED_RESIDENTIAL_FEE": "Residential fee not owed",
    "UNAUTHORIZED_ADDRESS_CORRECTION_FEE": "Address correction fee not owed",
    "UNAUTHORIZED_ACCESSORIAL_OTHER": "Other unauthorized fee",
    "DUPLICATE_BILLING_COLLISION": "Duplicate billing",
    "SLA_DELIVERY_FAILURE_REFUND": "Late delivery (service guarantee)",
    "FUEL_SURCHARGE_VARIANCE": "Fuel surcharge above contract",
}


@dataclass(frozen=True)
class EvidenceBlock:
    code: str
    title: str
    lines: Tuple[str, ...]


def _missing(value: Any) -> bool:
    # NaN and NaT are the only values that differ from themselves.
    return value is None or value != value


def _num(row: Mapping[str, Any], key: str) -> Optional[float]:
    value = row.get(key)
    return None if _missing(value) else float(value)


def _text(row: Mapping[str, Any], key: str) -> Optional[str]:
    value = row.get(key)
    if _missing(value):
        return None
    text = str(value).strip()
    return text or None


def _money(value: Optional[float]) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _lbs(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:,.1f} lb"


def _when(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if _missing(value):
        return "n/a"
    return (
        value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else str(value)
    )


def split_codes(anomaly_codes: Optional[str]) -> List[str]:
    """Reason codes as the mart writes them: ``CODE_A | CODE_B``."""
    if _missing(anomaly_codes) or not str(anomaly_codes).strip():
        return []
    return [part.strip() for part in str(anomaly_codes).split("|") if part.strip()]


def _weight(row: Mapping[str, Any]) -> List[str]:
    lines = [
        f"Carrier billed weight: {_lbs(_num(row, 'billed_weight'))}",
        "Expected billable weight: "
        f"{_lbs(_num(row, 'billable_weight_expected_lbs'))}"
        + (
            f" (basis: {_text(row, 'billable_weight_basis')})"
            if _text(row, "billable_weight_basis")
            else ""
        ),
        f"Warehouse scale weight: {_lbs(_num(row, 'actual_scale_weight'))}; "
        f"dimensional weight: {_lbs(_num(row, 'dim_weight_lbs'))}",
    ]
    excess = _num(row, "weight_variance_lbs")
    if excess is not None:
        lines.append(f"Billed above expected by {excess:+.1f} lb")
    return lines


def _base_rate(row: Mapping[str, Any]) -> List[str]:
    lines = [
        f"Billed base rate: {_money(_num(row, 'billed_base_rate_usd'))} "
        f"vs contract rate: {_money(_num(row, 'contract_base_rate_usd'))}",
    ]
    variance = _num(row, "base_rate_variance_usd")
    if variance is not None:
        lines.append(f"Difference: {_money(variance)}")
    zone = _text(row, "zone")
    tier_min, tier_max = _num(row, "weight_tier_min_lbs"), _num(
        row, "weight_tier_max_lbs"
    )
    if zone and tier_min is not None and tier_max is not None:
        tier = (
            f"{tier_min:g} lb"
            if tier_min == tier_max
            else f"{tier_min:g}-{tier_max:g} lb"
        )
        lines.append(f"Contract lookup: zone {zone}, {tier} tier")
    zone_billed = _text(row, "zone_billed")
    if zone and zone_billed and zone_billed != zone:
        lines.append(f"Carrier billed zone {zone_billed}; contract zone is {zone}")
    level, level_billed = _text(row, "service_level"), _text(
        row, "service_level_billed"
    )
    if level and level_billed and level != level_billed:
        lines.append(f"Carrier billed service {level_billed}; shipped as {level}")
    return lines


def _fuel(row: Mapping[str, Any]) -> List[str]:
    return [
        f"Billed fuel surcharge: {_money(_num(row, 'billed_fuel_surcharge_usd'))} "
        f"vs expected: {_money(_num(row, 'expected_fuel_surcharge_usd'))}",
        f"Difference: {_money(_num(row, 'fuel_surcharge_variance_usd'))}",
    ]


def _residential(row: Mapping[str, Any]) -> List[str]:
    return [
        "Residential surcharge billed: "
        f"{_money(_num(row, 'unauthorized_residential_fee_usd'))}",
        f"Warehouse record of the delivery address type: "
        f"{_text(row, 'dest_address_type') or 'n/a'}",
    ]


def _address_correction(row: Mapping[str, Any]) -> List[str]:
    validated = row.get("is_dest_address_cass_validated")
    status = "n/a" if _missing(validated) else ("yes" if bool(validated) else "no")
    return [
        "Address correction fee billed: "
        f"{_money(_num(row, 'unauthorized_address_correction_fee_usd'))}",
        f"Address already verified by USPS CASS software: {status}",
    ]


def _other_fee(row: Mapping[str, Any]) -> List[str]:
    total = _num(row, "unauthorized_accessorial_usd") or 0.0
    other = (
        total
        - (_num(row, "unauthorized_residential_fee_usd") or 0.0)
        - (_num(row, "unauthorized_address_correction_fee_usd") or 0.0)
    )
    lines = [f"Other fees the contract does not authorize: {_money(round(other, 2))}"]
    codes = _text(row, "unauthorized_accessorial_codes")
    if codes:
        lines.append(f"Fee codes on the invoice: {codes}")
    return lines


def _duplicate(row: Mapping[str, Any]) -> List[str]:
    count = _num(row, "distinct_invoice_number_count")
    lines = [
        f"Billed on {int(count) if count is not None else 'multiple'} invoice(s): "
        f"{_text(row, 'all_invoice_numbers') or 'n/a'}",
        f"Channels: {_text(row, 'all_source_channels') or 'n/a'}",
        f"Amount billed again: {_money(_num(row, 'duplicate_billed_amount_usd'))}",
    ]
    return lines


def _sla(row: Mapping[str, Any]) -> List[str]:
    late = _num(row, "delivery_variance_hours")
    return [
        f"Service purchased: {_text(row, 'service_level') or 'n/a'} (guaranteed)",
        f"Promised delivery by: {_when(row, 'guaranteed_delivery_timestamp')}",
        f"Actual delivery: {_when(row, 'actual_delivery_timestamp')}"
        + (f" ({late:.1f} h late)" if late is not None else ""),
        "Refund due under the guarantee: "
        f"{_money(_num(row, 'gsr_refund_usd'))} (transportation charge)",
    ]


_RENDERERS = {
    "WEIGHT_INFLATION": _weight,
    "BASE_RATE_OVERCHARGE": _base_rate,
    "UNAUTHORIZED_RESIDENTIAL_FEE": _residential,
    "UNAUTHORIZED_ADDRESS_CORRECTION_FEE": _address_correction,
    "UNAUTHORIZED_ACCESSORIAL_OTHER": _other_fee,
    "DUPLICATE_BILLING_COLLISION": _duplicate,
    "SLA_DELIVERY_FAILURE_REFUND": _sla,
}


def build_evidence(row: Mapping[str, Any]) -> List[EvidenceBlock]:
    """One block per reason code on the row, in the mart's gate order.

    Recoverable fuel variance has no reason code of its own in the mart, so it
    is shown as its own block whenever dollars are being recovered for it.
    """
    blocks = [
        EvidenceBlock(code, TITLES[code], tuple(_RENDERERS[code](row)))
        for code in split_codes(row.get("anomaly_classification"))
        if code in _RENDERERS
    ]
    if (_num(row, "fuel_recovery_usd") or 0) > 0:
        blocks.append(
            EvidenceBlock(
                "FUEL_SURCHARGE_VARIANCE",
                TITLES["FUEL_SURCHARGE_VARIANCE"],
                tuple(_fuel(row)),
            )
        )
    return blocks


def recovery_breakdown(row: Mapping[str, Any]) -> List[Tuple[str, float]]:
    """The mart's recoverable dollars by component, omitting zero parts."""
    components = (
        ("Guaranteed-service refund", "gsr_refund_usd"),
        ("Base rate", "base_rate_recovery_usd"),
        ("Fuel surcharge", "fuel_recovery_usd"),
        ("Unauthorized fees", "accessorial_recovery_usd"),
        ("Duplicate billing", "duplicate_recovery_usd"),
    )
    return [
        (label, value)
        for label, key in components
        if (value := _num(row, key)) is not None and value > 0
    ]


def evidence_lines(row: Mapping[str, Any]) -> List[str]:
    """Flat ``[CODE] fact`` lines for the dispute-letter prompt."""
    return [
        f"[{block.code}] {line}"
        for block in build_evidence(row)
        for line in block.lines
    ]
