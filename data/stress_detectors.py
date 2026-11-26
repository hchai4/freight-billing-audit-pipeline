"""Detectors for the five injected defect types, plus recall scoring.

Both detectors read only the generated CSVs (never the label file) in chunks;
``score`` then compares what they flagged with the generator's ground truth.

* ``reference`` -- the obvious rule for each defect, written from the format spec
  and importing the generator's own constants (invoice-ID pattern, fuel table,
  1-day ingestion SLA). On easy data it must reach 100%; that only shows the
  harness is wired correctly.
* ``blind`` -- imports nothing from the generator. It learns the ID shapes from
  the first chunk, the fuel rate for each month from the data's own median, and
  the lateness threshold from the spread of ingestion lags, and it normalises
  whitespace and case before comparing. Its misses and false positives on hard
  data are the informative numbers.

Neither is the dbt layer or an LLM. Treat results as a property of this synthetic
data and these rules, not as accuracy on real carrier invoices.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from data.stress_generator import (
    DECOY_TYPES,
    DUPLICATE_INVOICE,
    FUEL_PCT_BY_MONTH,
    FUEL_TIER_JUMP,
    INVOICE_ID_PATTERN,
    ISSUE_TYPES,
    LATE_ARRIVAL,
    MISSING_KEY,
    TRUNCATED_FIELD,
)

Findings = Dict[Tuple[str, str], np.ndarray]

DETECTORS = ("reference", "blind")
LATE_ARRIVAL_THRESHOLD = pd.Timedelta(days=1)
FUEL_TOLERANCE_CENTS = 1
MISSING_TEXT = {"", "NULL", "NONE", "NAN"}
TS_FORMAT = "%Y-%m-%d %H:%M:%S"
MAD_TO_SIGMA = 1.4826
LATE_SIGMAS = 10  # lateness threshold = median lag + 10 robust sigmas
EMPTY = np.array([], dtype=np.int64)


def _read(path: Path, chunk_rows: int, **kwargs):
    return pd.read_csv(
        path, dtype=str, keep_default_na=False, chunksize=chunk_rows, **kwargs
    )


def _cents(series: pd.Series) -> np.ndarray:
    return np.rint(series.astype(float).to_numpy() * 100).astype(np.int64)


def _concat(parts) -> np.ndarray:
    return np.concatenate(parts) if parts else EMPTY


def _hash(series: pd.Series) -> np.ndarray:
    return pd.util.hash_pandas_object(series, index=False).to_numpy()


def _first_repeat_mask(keys: np.ndarray) -> np.ndarray:
    """True for every occurrence of a key after its first."""
    return pd.Series(keys).duplicated(keep="first").to_numpy()


# --------------------------------------------------------------------- reference


def audit_wms_reference(path: Path, chunk_rows: int) -> Findings:
    """Late arrivals: warehouse feed lands more than a day after the scan."""
    late_rows, offset = [], 0
    for chunk in _read(path, chunk_rows):
        scan = pd.to_datetime(chunk["scan_timestamp"], format=TS_FORMAT)
        arrived = pd.to_datetime(chunk["ingestion_timestamp"], format=TS_FORMAT)
        late = ((arrived - scan) > LATE_ARRIVAL_THRESHOLD).to_numpy()
        late_rows.append(offset + np.flatnonzero(late))
        offset += len(chunk)
    return {("wms", LATE_ARRIVAL): _concat(late_rows)}


def audit_edi_reference(path: Path, chunk_rows: int) -> Findings:
    """Missing keys, truncated invoice IDs, fuel-tier jumps and duplicate billing."""
    missing, truncated, jumps, key_hashes, key_rows = [], [], [], [], []
    offset = 0
    for chunk in _read(path, chunk_rows):
        rows = offset + np.arange(len(chunk))
        gone = (
            chunk["tracking_id"].str.strip().str.upper().isin(MISSING_TEXT).to_numpy()
        )
        missing.append(rows[gone])

        bad_id = ~chunk["carrier_invoice_id"].str.fullmatch(INVOICE_ID_PATTERN)
        truncated.append(rows[bad_id.to_numpy()])

        month = chunk["invoice_date"].str.slice(5, 7).astype(int).to_numpy() - 1
        expected_fuel = np.rint(_cents(chunk["base_rate"]) * FUEL_PCT_BY_MONTH[month])
        off_tier = np.abs(_cents(chunk["fuel_surcharge"]) - expected_fuel)
        jumps.append(rows[off_tier > FUEL_TOLERANCE_CENTS])

        # A blank key cannot be matched to anything, so it is not a duplicate.
        keyed = ~gone
        pair = (
            chunk.loc[keyed, "tracking_id"]
            + "|"
            + chunk.loc[keyed, "carrier_invoice_id"]
        )
        key_hashes.append(_hash(pair))
        key_rows.append(rows[keyed])
        offset += len(chunk)

    ids = _concat(key_rows)
    return {
        ("edi", MISSING_KEY): _concat(missing),
        ("edi", TRUNCATED_FIELD): _concat(truncated),
        ("edi", FUEL_TIER_JUMP): _concat(jumps),
        ("edi", DUPLICATE_INVOICE): ids[_first_repeat_mask(_concat(key_hashes))],
    }


# ------------------------------------------------------------------------- blind


def _normalise(series: pd.Series) -> pd.Series:
    return series.str.strip().str.upper()


def _mode_len(lengths: pd.Series) -> int:
    return int(lengths.mode().iloc[0])


def audit_wms_blind(path: Path, chunk_rows: int) -> Findings:
    """Flag lags far above the typical lag, with the cut-off learned from the data."""
    lags = []
    for chunk in _read(
        path, chunk_rows, usecols=["scan_timestamp", "ingestion_timestamp"]
    ):
        scan = pd.to_datetime(chunk["scan_timestamp"], format=TS_FORMAT)
        arrived = pd.to_datetime(chunk["ingestion_timestamp"], format=TS_FORMAT)
        lags.append((arrived - scan).dt.total_seconds().to_numpy())
    lag = _concat(lags)
    median = np.median(lag)
    sigma = MAD_TO_SIGMA * np.median(np.abs(lag - median))
    return {("wms", LATE_ARRIVAL): np.flatnonzero(lag > median + LATE_SIGMAS * sigma)}


def wms_tracking_hashes(path: Path, chunk_rows: int) -> np.ndarray:
    hashes = [
        _hash(_normalise(chunk["tracking_id"]))
        for chunk in _read(path, chunk_rows, usecols=["tracking_id"])
    ]
    return np.sort(_concat(hashes))


def audit_edi_blind(
    path: Path, chunk_rows: int, known_tracking: Optional[np.ndarray] = None
) -> Findings:
    """Learn shapes and rates from the data, then flag what does not fit."""
    parts = {
        k: []
        for k in (
            "track_hash",
            "track_len",
            "track_prefix",
            "invoice_len",
            "total",
            "base",
            "fuel",
            "month",
            "pair_hash",
        )
    }
    for chunk in _read(path, chunk_rows):
        track = _normalise(chunk["tracking_id"])
        invoice = _normalise(chunk["carrier_invoice_id"])
        parts["track_hash"].append(_hash(track))
        parts["track_len"].append(track.str.len().to_numpy())
        parts["track_prefix"].append(track.str.slice(0, 2).to_numpy())
        parts["invoice_len"].append(invoice.str.len().to_numpy())
        parts["total"].append(_cents(chunk["total_billed_amount"]))
        parts["base"].append(_cents(chunk["base_rate"]))
        parts["fuel"].append(_cents(chunk["fuel_surcharge"]))
        parts["month"].append(
            chunk["invoice_date"].str.slice(5, 7).astype(int).to_numpy() - 1
        )
        parts["pair_hash"].append(_hash(track + "|" + invoice))
    a = {k: _concat(v) for k, v in parts.items()}
    n = len(a["total"])
    rows = np.arange(n)

    # Shapes: whatever is most common is "valid".
    valid_len = _mode_len(pd.Series(a["track_len"]))
    valid_prefix = pd.Series(a["track_prefix"]).mode().iloc[0]
    bad_shape = (a["track_len"] != valid_len) | (a["track_prefix"] != valid_prefix)
    orphan = (
        ~np.isin(a["track_hash"], known_tracking)
        if known_tracking is not None
        else np.zeros(n, dtype=bool)
    )
    missing = bad_shape | orphan

    invoice_valid_len = _mode_len(pd.Series(a["invoice_len"]))
    truncated = a["invoice_len"] != invoice_valid_len

    # Fuel: each month's rate is the median fuel/base ratio seen that month.
    off_rate = np.zeros(n, dtype=bool)
    ratio = a["fuel"] / np.maximum(a["base"], 1)
    for month in np.unique(a["month"]):
        in_month = a["month"] == month
        rate = np.median(ratio[in_month])
        expected = np.rint(a["base"][in_month] * rate)
        off_rate[in_month] = (
            np.abs(a["fuel"][in_month] - expected) > FUEL_TOLERANCE_CENTS
        )

    # Duplicate = same shipment, same invoice and same amount. A second line with
    # a different amount is a legitimate adjustment, not a duplicate.
    key = a["pair_hash"] ^ (
        a["total"].astype(np.uint64) * np.uint64(0x9E3779B97F4A7C15)
    )
    keyed = ~missing
    duplicated = np.zeros(n, dtype=bool)
    duplicated[rows[keyed]] = _first_repeat_mask(key[keyed])

    return {
        ("edi", MISSING_KEY): rows[missing],
        ("edi", TRUNCATED_FIELD): rows[truncated],
        ("edi", FUEL_TIER_JUMP): rows[off_rate],
        ("edi", DUPLICATE_INVOICE): rows[duplicated],
    }


# ----------------------------------------------------------------------- scoring


def _ratio(num: int, den: int) -> Optional[float]:
    return round(num / den, 6) if den else None


def score(findings: Findings, labels_path: Path) -> dict:
    """Recall and precision per defect and per variant, plus false positives by decoy."""
    labels = pd.read_csv(labels_path, keep_default_na=False)
    per_issue, per_variant = {}, {}
    for issue in ISSUE_TYPES:
        dataset = "wms" if issue == LATE_ARRIVAL else "edi"
        flagged = np.unique(findings.get((dataset, issue), EMPTY))
        truth = labels[(labels["dataset"] == dataset) & (labels["issue_type"] == issue)]
        caught_mask = truth["row_id"].isin(flagged)
        hits = int(caught_mask.sum())
        per_issue[issue] = {
            "injected": int(len(truth)),
            "flagged": int(len(flagged)),
            "true_positives": hits,
            "false_negatives": int(len(truth) - hits),
            "false_positives": int(len(flagged) - hits),
            "recall": _ratio(hits, len(truth)),
            "precision": _ratio(hits, len(flagged)),
        }
        for variant, group in truth.groupby("variant"):
            caught = int(caught_mask[group.index].sum())
            per_variant[f"{issue}/{variant}"] = {
                "injected": int(len(group)),
                "caught": caught,
                "recall": _ratio(caught, len(group)),
            }

    flagged_by_dataset = {
        ds: np.unique(_concat([v for (d, _), v in findings.items() if d == ds]))
        for ds in ("wms", "edi")
    }
    decoy_false_positives = {}
    for decoy in DECOY_TYPES:
        rows = labels[labels["issue_type"] == decoy]
        if rows.empty:
            continue
        ds = rows["dataset"].iloc[0]
        wrongly = int(rows["row_id"].isin(flagged_by_dataset[ds]).sum())
        decoy_false_positives[decoy] = {
            "present": int(len(rows)),
            "wrongly_flagged": wrongly,
        }
    return {
        "per_issue": per_issue,
        "per_variant": per_variant,
        "decoy_false_positives": decoy_false_positives,
    }


def run_audit(
    input_dir: Path, chunk_rows: int = 250_000, detector: str = "reference"
) -> dict:
    if detector not in DETECTORS:
        raise ValueError(f"detector must be one of {DETECTORS}")
    manifest = json.loads((input_dir / "manifest.json").read_text())
    files = {k: Path(v) for k, v in manifest["files"].items()}
    started = time.perf_counter()
    if detector == "reference":
        findings = {
            **audit_wms_reference(files["wms"], chunk_rows),
            **audit_edi_reference(files["edi"], chunk_rows),
        }
    else:
        known = wms_tracking_hashes(files["wms"], chunk_rows)
        findings = {
            **audit_wms_blind(files["wms"], chunk_rows),
            **audit_edi_blind(files["edi"], chunk_rows, known),
        }
    elapsed = time.perf_counter() - started
    scored = score(findings, files["labels"])
    per_issue = scored["per_issue"]
    injected = sum(v["injected"] for v in per_issue.values())
    caught = sum(v["true_positives"] for v in per_issue.values())
    flagged = sum(v["flagged"] for v in per_issue.values())
    return {
        "detector": detector,
        "difficulty": manifest["config"]["difficulty"],
        "rows_scanned": manifest["total_rows"],
        "audit_seconds": round(elapsed, 3),
        "rows_per_second": round(manifest["total_rows"] / elapsed),
        "overall_recall": _ratio(caught, injected),
        "overall_precision": _ratio(caught, flagged),
        **scored,
    }


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Run a detector and score recall.")
    p.add_argument("--input-dir", type=Path, default=Path("data/stress_output"))
    p.add_argument("--chunk-rows", type=int, default=250_000)
    p.add_argument("--detector", choices=DETECTORS, default="reference")
    p.add_argument("--output", type=Path, help="write the report as JSON")
    args = p.parse_args(argv)
    report = run_audit(args.input_dir, args.chunk_rows, args.detector)
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
