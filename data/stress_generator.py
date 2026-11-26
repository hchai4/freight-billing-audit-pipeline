"""Synthetic WMS + UPS EDI generator for stress-testing the reconciliation pipeline.

Default run: 2,000,000 WMS scan rows + 2,000,000 EDI invoice rows = 4,000,000
rows. Rows are generated and written in fixed-size chunks, so peak memory is set
by ``--chunk-rows``, not by the total row count. Output is a pure function of
``(seed, rows, chunk_rows, rates)``: the same arguments always produce the same
files.

Five defect types are injected, each recorded in a ground-truth label file:

* DUPLICATE_INVOICE  EDI row repeats an earlier row's tracking ID + invoice ID
                     under a new ``edi_record_id`` (the carrier billed it twice).
* LATE_ARRIVAL       WMS row reaches the warehouse feed 35-90 days after the
                     scan, always in a later calendar month.
* MISSING_KEY        EDI ``tracking_id`` is blank, whitespace or the text NULL.
* TRUNCATED_FIELD    EDI ``carrier_invoice_id`` is cut to 8 characters, as when a
                     fixed-width field overflows.
* FUEL_TIER_JUMP     EDI fuel surcharge uses a rate 3-6 points above the
                     carrier's published rate for that invoice month; the total is
                     recomputed so only the tier check can catch it.

Two difficulty levels share the same schema:

* ``easy`` (baseline): one fixed, obvious form per defect; a row carries at most
  one defect; no look-alike rows. Any rule written from the format spec finds
  everything, so scores here only prove the harness is wired correctly.
* ``hard``: each defect comes in several variants (formatting noise, small fuel
  drifts, borderline lateness, placeholder keys, variable-length truncation),
  about 10% of defective EDI rows carry a second defect, and unlabelled
  look-alike rows ("decoys") that are legitimate are mixed in to punish
  over-eager rules. The label file records each defect's ``variant`` and lists
  the decoys as DECOY_* rows so false positives can be traced to their cause.

In easy mode defects are mutually exclusive within a table and duplicates only
copy clean rows, so every label is unambiguous.
"""

from __future__ import annotations

import argparse
import json
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Dict, List

import gzip

import numpy as np
import pandas as pd

DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
LATE_ARRIVAL = "LATE_ARRIVAL"
MISSING_KEY = "MISSING_KEY"
TRUNCATED_FIELD = "TRUNCATED_FIELD"
FUEL_TIER_JUMP = "FUEL_TIER_JUMP"
ISSUE_TYPES = (
    DUPLICATE_INVOICE,
    LATE_ARRIVAL,
    MISSING_KEY,
    TRUNCATED_FIELD,
    FUEL_TIER_JUMP,
)

WMS_COLUMNS = [
    "package_id",
    "tracking_id",
    "carrier_code",
    "service_level",
    "actual_scale_weight",
    "package_length_in",
    "package_width_in",
    "package_height_in",
    "origin_warehouse",
    "origin_zip",
    "dest_zip",
    "dest_address_type",
    "dest_address_cass_validated",
    "scan_timestamp",
    "dock_departure_timestamp",
    "expected_cost",
    "ingestion_timestamp",
]
EDI_COLUMNS = [
    "edi_record_id",
    "tracking_id",
    "carrier_invoice_id",
    "billed_weight",
    "base_rate",
    "fuel_surcharge",
    "total_billed_amount",
    "service_level_billed",
    "zone_billed",
    "dest_zip",
    "delivery_timestamp",
    "invoice_date",
]
LABEL_COLUMNS = ["dataset", "row_id", "issue_type", "related_row_id", "variant"]

# Legitimate look-alikes, hard mode only. They are not defects.
DECOY_SLOW_BATCH = "DECOY_SLOW_BATCH"  # WMS feed lands 6-20 h after the scan
DECOY_SPLIT_LINE = "DECOY_SPLIT_LINE"  # second invoice line, same tracking + invoice
DECOY_GRACE_RATE = "DECOY_GRACE_RATE"  # early-month invoice at last month's fuel rate
DECOY_TYPES = (DECOY_SLOW_BATCH, DECOY_SPLIT_LINE, DECOY_GRACE_RATE)

DIFFICULTIES = ("easy", "hard")
STANDARD = "standard"
LEGIT = "legit"

# Hard-mode variants: (name, probability).
DUPLICATE_VARIANTS = [("exact", 0.65), ("formatted", 0.35)]
LATE_VARIANTS = [("cross_month", 0.4), ("days_late", 0.4), ("hours_late", 0.2)]
MISSING_VARIANTS = [("blank", 0.5), ("sentinel", 0.3), ("placeholder", 0.2)]
TRUNCATION_VARIANTS = [("cut", 0.7), ("padded", 0.3)]
FUEL_VARIANTS = [("major", 0.5), ("one_tier", 0.3), ("minor", 0.2)]
SENTINEL_KEYS = np.array(["N/A", "UNKNOWN", "TBD", "0"])
ONE_TIER_STEP = 0.0025  # one step of the published fuel table
MINOR_FUEL_STEP = 0.001
GRACE_DAYS = 5  # early-month invoices that may carry the previous month's rate

TRACKING_LENGTH = 18  # "1Z999000" + 10 digits
INVOICE_ID_PATTERN = r"UPSINV\d{10}"
TRUNCATED_INVOICE_LENGTH = 8

# The carrier's published fuel-surcharge rate by invoice month (Jan..Dec).
FUEL_PCT_BY_MONTH = np.array(
    [0.1225, 0.1250, 0.1275, 0.1300, 0.1325, 0.1350]
    + [0.1375, 0.1400, 0.1425, 0.1450, 0.1475, 0.1500]
)
FUEL_JUMP_POINTS = np.array([0.03, 0.04, 0.05, 0.06])
MISSING_KEY_VALUES = np.array(["", "   ", "NULL"])

WAREHOUSES = [
    ("ONT8_CALIFORNIA", "91761"),
    ("LAX9_CALIFORNIA", "90245"),
    ("PHX6_ARIZONA", "85043"),
    ("DFW7_TEXAS", "75063"),
]
ROUTES = [
    ("10001", 8),
    ("60601", 5),
    ("77001", 4),
    ("85001", 3),
    ("94102", 3),
    ("98001", 6),
    ("30301", 5),
    ("33101", 7),
]
SERVICES = ["GROUND", "SECOND_DAY_AIR", "NEXT_DAY_AIR"]
SERVICE_PROBABILITY = [0.80, 0.15, 0.05]
SERVICE_MULTIPLIER = np.array([1.00, 1.65, 2.35])
SERVICE_TRANSIT_HOURS = np.array([120, 48, 24])
ADDRESS_TYPES = ["RESIDENTIAL", "COMMERCIAL"]

SECONDS_PER_DAY = 86_400
LATE_ARRIVAL_MIN_DAYS = 35  # > 31, so the row always lands in a later month
LATE_ARRIVAL_MAX_DAYS = 90
NORMAL_INGESTION_MAX_SECONDS = 6 * 3600


@dataclass(frozen=True)
class GenerationConfig:
    rows: int = 2_000_000  # per table; total = 2 * rows
    seed: int = 42
    chunk_rows: int = 250_000
    start: str = "2025-01-01"
    span_days: int = 365
    output_dir: Path = Path("data/stress_output")
    duplicate_rate: float = 0.03
    late_arrival_rate: float = 0.02
    missing_key_rate: float = 0.015
    truncation_rate: float = 0.02
    fuel_jump_rate: float = 0.005
    gzip_output: bool = False
    difficulty: str = "easy"
    # Hard mode only
    stack_rate: float = 0.10  # share of defective EDI rows that get a 2nd defect
    slow_batch_rate: float = 0.04  # share of WMS rows (decoys)
    split_line_rate: float = 0.02  # share of EDI rows (decoys)
    grace_rate: float = 0.06  # share of early-month EDI rows (decoys)

    def edi_defect_total(self) -> float:
        return (
            self.duplicate_rate
            + self.missing_key_rate
            + self.truncation_rate
            + self.fuel_jump_rate
        )


def _ids(prefix: str, numbers: np.ndarray, width: int) -> np.ndarray:
    return (prefix + pd.Series(numbers).astype(str).str.zfill(width)).to_numpy()


def _pick(rng, variants, count: int) -> np.ndarray:
    names, probabilities = zip(*variants)
    return rng.choice(np.array(names, dtype=object), count, p=probabilities)


def _to_ts(seconds: np.ndarray) -> pd.Series:
    return pd.to_datetime(pd.Series(seconds), unit="s")


def build_chunk(cfg: GenerationConfig, chunk_index: int, start: int, n: int):
    """One chunk of both tables plus its labels; depends only on its arguments."""
    rng = np.random.default_rng([cfg.seed, chunk_index])
    index = np.arange(start, start + n, dtype=np.int64)

    # ---- shared shipment facts ------------------------------------------------
    service = rng.choice(3, n, p=SERVICE_PROBABILITY)
    warehouse = rng.integers(0, len(WAREHOUSES), n)
    route = rng.integers(0, len(ROUTES), n)
    weight_hundredths = rng.integers(50, 7_000, n)  # 0.50 - 70.00 lb
    dims = rng.uniform([8, 6, 4], [16, 12, 10], size=(n, 3)).round(2)
    first = int(pd.Timestamp(cfg.start).timestamp())
    scan = first + rng.integers(0, cfg.span_days * SECONDS_PER_DAY, n)
    dock = scan + rng.integers(3_600, 12 * 3_600, n)
    delivery = (
        scan
        + SERVICE_TRANSIT_HOURS[service] * 3_600
        + rng.integers(-6 * 3_600, 12 * 3_600, n)
    )
    invoice_day = delivery // SECONDS_PER_DAY + rng.integers(1, 8, n)
    invoice_date = invoice_day.astype("datetime64[D]")
    invoice_month = invoice_date.astype("datetime64[M]").astype(int) % 12

    zone = np.array([r[1] for r in ROUTES])[route]
    weight_lb = weight_hundredths / 100.0
    base_cents = np.rint(
        (5.5 + 0.9 * zone + 0.45 * weight_lb) * SERVICE_MULTIPLIER[service] * 100
    ).astype(np.int64)
    fuel_pct = FUEL_PCT_BY_MONTH[invoice_month]
    fuel_cents = np.rint(base_cents * fuel_pct).astype(np.int64)
    dest_zip = np.array([r[0] for r in ROUTES])[route]

    tracking = _ids("1Z999000", index, 10)

    # ---- WMS table ------------------------------------------------------------
    late = rng.random(n) < cfg.late_arrival_rate
    ingest_lag = rng.integers(60, NORMAL_INGESTION_MAX_SECONDS, n)
    ingest_lag[late] = rng.integers(
        LATE_ARRIVAL_MIN_DAYS * SECONDS_PER_DAY,
        LATE_ARRIVAL_MAX_DAYS * SECONDS_PER_DAY,
        int(late.sum()),
    )
    hard = cfg.difficulty == "hard"
    late_variant = np.full(n, STANDARD, dtype=object)
    slow = np.zeros(n, dtype=bool)
    if hard:
        late_variant[late] = _pick(rng, LATE_VARIANTS, int(late.sum()))
        days = late_variant == "days_late"
        hours = late_variant == "hours_late"
        ingest_lag[days] = rng.integers(
            2 * SECONDS_PER_DAY, 20 * SECONDS_PER_DAY, int(days.sum())
        )
        ingest_lag[hours] = rng.integers(22 * 3600, 48 * 3600, int(hours.sum()))
        slow = ~late & (rng.random(n) < cfg.slow_batch_rate)
        ingest_lag[slow] = rng.integers(
            NORMAL_INGESTION_MAX_SECONDS, 20 * 3600, int(slow.sum())
        )
    wms = pd.DataFrame(
        {
            "package_id": _ids("PKG-", index, 8),
            "tracking_id": tracking,
            "carrier_code": "UPS",
            "service_level": np.array(SERVICES)[service],
            "actual_scale_weight": weight_lb,
            "package_length_in": dims[:, 0],
            "package_width_in": dims[:, 1],
            "package_height_in": dims[:, 2],
            "origin_warehouse": np.array([w[0] for w in WAREHOUSES])[warehouse],
            "origin_zip": np.array([w[1] for w in WAREHOUSES])[warehouse],
            "dest_zip": dest_zip,
            "dest_address_type": np.array(ADDRESS_TYPES)[rng.integers(0, 2, n)],
            "dest_address_cass_validated": rng.random(n) < 0.97,
            "scan_timestamp": _to_ts(scan),
            "dock_departure_timestamp": _to_ts(dock),
            "expected_cost": (base_cents + fuel_cents) / 100.0,
            "ingestion_timestamp": _to_ts(scan + ingest_lag),
        }
    )[WMS_COLUMNS]

    # ---- EDI table ------------------------------------------------------------
    edi = pd.DataFrame(
        {
            "edi_record_id": _ids("EDI-", index, 8),
            "tracking_id": tracking,
            "carrier_invoice_id": _ids("UPSINV", index // 500, 10),
            "billed_weight": weight_lb,
            "base_rate": base_cents / 100.0,
            "fuel_surcharge": fuel_cents / 100.0,
            "total_billed_amount": (base_cents + fuel_cents) / 100.0,
            "service_level_billed": np.array(SERVICES)[service],
            "zone_billed": zone,
            "dest_zip": dest_zip,
            "delivery_timestamp": _to_ts(delivery),
            "invoice_date": pd.Series(invoice_date),
        }
    )[EDI_COLUMNS]

    # One uniform draw picks at most one primary EDI defect per row.
    edges = np.cumsum(
        [
            cfg.duplicate_rate,
            cfg.missing_key_rate,
            cfg.truncation_rate,
            cfg.fuel_jump_rate,
        ]
    )
    kind = np.searchsorted(edges, rng.random(n), side="right")
    DUP, MISS, TRUNC, JUMP, CLEAN = 0, 1, 2, 3, 4

    labels: List[pd.DataFrame] = []

    def add_labels(dataset, rows, issue, related=None, variant=STANDARD):
        if len(rows):
            labels.append(
                pd.DataFrame(
                    {
                        "dataset": dataset,
                        "row_id": start + rows,
                        "issue_type": issue,
                        "related_row_id": (-1 if related is None else start + related),
                        "variant": variant,
                    }
                )
            )

    # Duplicates copy a clean row that appears earlier in the chunk.
    clean_pos = np.flatnonzero(kind == CLEAN)
    dup_pos = np.flatnonzero(kind == DUP)
    earlier_clean = np.searchsorted(clean_pos, dup_pos)
    has_source = earlier_clean > 0
    kind[dup_pos[~has_source]] = CLEAN  # too early in the chunk to copy anything
    dup_pos, earlier_clean = dup_pos[has_source], earlier_clean[has_source]
    source = clean_pos[(rng.random(len(dup_pos)) * earlier_clean).astype(np.int64)]
    copy_columns = [c for c in EDI_COLUMNS if c != "edi_record_id"]

    secondary = np.full(n, CLEAN)
    if hard:
        # About stack_rate of defective rows get a second, different defect.
        stacked = (kind != CLEAN) & (rng.random(n) < cfg.stack_rate)
        other = rng.integers(MISS, JUMP + 1, n)
        other = np.where(other == kind, other % 3 + 1, other)
        secondary = np.where(stacked, other, CLEAN)

        # Decoys are applied first so duplicates copy them consistently.
        day = edi["invoice_date"].dt.day.to_numpy()
        eligible = kind == CLEAN
        grace_pos = np.flatnonzero(
            eligible & (day <= GRACE_DAYS) & (rng.random(n) < cfg.grace_rate)
        )
        eligible[grace_pos] = False
        prior_pct = FUEL_PCT_BY_MONTH[(invoice_month[grace_pos] - 1) % 12]
        grace_fuel = np.rint(base_cents[grace_pos] * prior_pct).astype(np.int64)
        edi.loc[edi.index[grace_pos], "fuel_surcharge"] = grace_fuel / 100.0
        edi.loc[edi.index[grace_pos], "total_billed_amount"] = (
            base_cents[grace_pos] + grace_fuel
        ) / 100.0
        add_labels("edi", grace_pos, DECOY_GRACE_RATE, variant=LEGIT)

        split_pos = np.flatnonzero(eligible & (rng.random(n) < cfg.split_line_rate))
        # Origins are never themselves split rows, so each split shares its
        # tracking and invoice with the row its label points to.
        pool = np.setdiff1d(clean_pos, split_pos)
        before = np.searchsorted(pool, split_pos)
        split_pos, before = split_pos[before > 0], before[before > 0]
        origin = pool[(rng.random(len(split_pos)) * before).astype(np.int64)]
        edi.loc[edi.index[split_pos], copy_columns] = edi.iloc[origin][
            copy_columns
        ].to_numpy()
        line_base = np.rint(
            np.rint(edi["base_rate"].iloc[origin].to_numpy() * 100)
            * rng.uniform(0.15, 0.6, len(split_pos))
        ).astype(np.int64)
        line_month = edi["invoice_date"].iloc[origin].dt.month.to_numpy() - 1
        line_fuel = np.rint(line_base * FUEL_PCT_BY_MONTH[line_month]).astype(np.int64)
        edi.loc[edi.index[split_pos], "base_rate"] = line_base / 100.0
        edi.loc[edi.index[split_pos], "fuel_surcharge"] = line_fuel / 100.0
        edi.loc[edi.index[split_pos], "total_billed_amount"] = (
            line_base + line_fuel
        ) / 100.0
        add_labels("edi", split_pos, DECOY_SPLIT_LINE, origin, variant=LEGIT)

    def positions(code):
        return np.flatnonzero((kind == code) | (secondary == code))

    edi.loc[edi.index[dup_pos], copy_columns] = edi.iloc[source][
        copy_columns
    ].to_numpy()
    dup_variant = STANDARD
    if hard:
        dup_variant = _pick(rng, DUPLICATE_VARIANTS, len(dup_pos))
        noisy = dup_pos[dup_variant == "formatted"]
        spaced = rng.integers(0, 2, len(noisy)) == 0
        for rows, column, fix in (
            (noisy[spaced], "tracking_id", lambda s: s + " "),
            (noisy[~spaced], "carrier_invoice_id", lambda s: s.str.lower()),
        ):
            edi.loc[edi.index[rows], column] = fix(edi[column].iloc[rows]).to_numpy()
    add_labels("edi", dup_pos, DUPLICATE_INVOICE, source, dup_variant)

    miss_pos = positions(MISS)
    keys = MISSING_KEY_VALUES[
        rng.integers(0, len(MISSING_KEY_VALUES), len(miss_pos))
    ].astype(object)
    miss_variant = STANDARD
    if hard:
        miss_variant = _pick(rng, MISSING_VARIANTS, len(miss_pos))
        sentinel = miss_variant == "sentinel"
        keys[sentinel] = SENTINEL_KEYS[
            rng.integers(0, len(SENTINEL_KEYS), int(sentinel.sum()))
        ]
        fake = miss_variant == "placeholder"
        # Well-formed but absent from the WMS feed: only a join can expose it.
        keys[fake] = _ids("1Z999999", rng.integers(0, 10**9, int(fake.sum())), 10)
    edi.loc[edi.index[miss_pos], "tracking_id"] = keys
    add_labels("edi", miss_pos, MISSING_KEY, variant=miss_variant)

    trunc_pos = positions(TRUNC)
    invoice_ids = edi["carrier_invoice_id"].iloc[trunc_pos]
    trunc_variant = STANDARD
    if hard:
        trunc_variant = _pick(rng, TRUNCATION_VARIANTS, len(trunc_pos))
        keep = rng.integers(TRUNCATED_INVOICE_LENGTH, 16, len(trunc_pos))
        cut = [s[:k] for s, k in zip(invoice_ids, keep)]
        cut = [
            c.ljust(len(s)) if v == "padded" else c
            for c, s, v in zip(cut, invoice_ids, trunc_variant)
        ]
        shortened = np.array(cut, dtype=object)
    else:
        shortened = invoice_ids.str.slice(0, TRUNCATED_INVOICE_LENGTH).to_numpy()
    edi.loc[edi.index[trunc_pos], "carrier_invoice_id"] = shortened
    add_labels("edi", trunc_pos, TRUNCATED_FIELD, variant=trunc_variant)

    jump_pos = positions(JUMP)
    now_base = np.rint(edi["base_rate"].iloc[jump_pos].to_numpy() * 100).astype(
        np.int64
    )
    now_month = edi["invoice_date"].iloc[jump_pos].dt.month.to_numpy() - 1
    step = rng.choice(FUEL_JUMP_POINTS, len(jump_pos))
    jump_variant = STANDARD
    if hard:
        jump_variant = _pick(rng, FUEL_VARIANTS, len(jump_pos))
        step = np.where(jump_variant == "one_tier", ONE_TIER_STEP, step)
        step = np.where(jump_variant == "minor", MINOR_FUEL_STEP, step)
    jumped_fuel = np.rint(now_base * (FUEL_PCT_BY_MONTH[now_month] + step)).astype(
        np.int64
    )
    edi.loc[edi.index[jump_pos], "fuel_surcharge"] = jumped_fuel / 100.0
    edi.loc[edi.index[jump_pos], "total_billed_amount"] = (
        now_base + jumped_fuel
    ) / 100.0
    add_labels("edi", jump_pos, FUEL_TIER_JUMP, variant=jump_variant)

    late_pos = np.flatnonzero(late)
    add_labels("wms", late_pos, LATE_ARRIVAL, variant=late_variant[late_pos])
    add_labels("wms", np.flatnonzero(slow), DECOY_SLOW_BATCH, variant=LEGIT)

    label_frame = (
        pd.concat(labels, ignore_index=True)
        if labels
        else pd.DataFrame(columns=LABEL_COLUMNS)
    )
    return wms, edi, label_frame[LABEL_COLUMNS]


def _open(path: Path, stack: ExitStack, gzip_output: bool) -> IO[str]:
    if gzip_output:
        return stack.enter_context(gzip.open(path, "wt", newline="", compresslevel=3))
    return stack.enter_context(open(path, "w", newline=""))


def output_paths(cfg: GenerationConfig) -> Dict[str, Path]:
    suffix = ".csv.gz" if cfg.gzip_output else ".csv"
    return {
        "wms": cfg.output_dir / f"wms_package_scans{suffix}",
        "edi": cfg.output_dir / f"ups_edi_invoices{suffix}",
        "labels": cfg.output_dir / f"defect_labels{suffix}",
        "manifest": cfg.output_dir / "manifest.json",
    }


def generate(cfg: GenerationConfig, progress: bool = False) -> dict:
    if cfg.rows <= 0 or cfg.chunk_rows <= 0:
        raise ValueError("rows and chunk_rows must be positive")
    if cfg.edi_defect_total() >= 1:
        raise ValueError("EDI defect rates must sum to less than 1")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    paths = output_paths(cfg)
    if cfg.difficulty not in DIFFICULTIES:
        raise ValueError(f"difficulty must be one of {DIFFICULTIES}")
    counts = {issue: 0 for issue in ISSUE_TYPES}
    decoys = {decoy: 0 for decoy in DECOY_TYPES}
    started = time.perf_counter()

    with ExitStack() as stack:
        wms_f = _open(paths["wms"], stack, cfg.gzip_output)
        edi_f = _open(paths["edi"], stack, cfg.gzip_output)
        label_f = _open(paths["labels"], stack, cfg.gzip_output)
        done, chunk_index = 0, 0
        while done < cfg.rows:
            n = min(cfg.chunk_rows, cfg.rows - done)
            wms, edi, labels = build_chunk(cfg, chunk_index, done, n)
            first = chunk_index == 0
            fmt = {"date_format": "%Y-%m-%d %H:%M:%S", "index": False}
            wms.to_csv(wms_f, header=first, float_format="%.2f", **fmt)
            edi["invoice_date"] = edi["invoice_date"].dt.strftime("%Y-%m-%d")
            edi.to_csv(edi_f, header=first, float_format="%.2f", **fmt)
            labels.to_csv(label_f, header=first, index=False)
            for issue, count in labels["issue_type"].value_counts().items():
                (decoys if issue in decoys else counts)[issue] += int(count)
            done += n
            chunk_index += 1
            if progress:
                print(f"  generated {done:>9,} / {cfg.rows:,} rows per table")

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows_per_table": cfg.rows,
        "total_rows": 2 * cfg.rows,
        "chunks": chunk_index,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "defects_injected": counts,
        "decoys_injected": decoys,
        "config": {
            k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(cfg).items()
        },
        "files": {k: str(v) for k, v in paths.items() if k != "manifest"},
    }
    paths["manifest"].write_text(json.dumps(manifest, indent=2))
    return manifest


def parse_args(argv=None) -> GenerationConfig:
    d = GenerationConfig()
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--rows", type=int, default=d.rows, help="rows per table")
    p.add_argument("--seed", type=int, default=d.seed)
    p.add_argument("--chunk-rows", type=int, default=d.chunk_rows)
    p.add_argument("--output-dir", type=Path, default=d.output_dir)
    p.add_argument("--gzip", action="store_true", help="write .csv.gz files")
    p.add_argument("--duplicate-rate", type=float, default=d.duplicate_rate)
    p.add_argument("--late-arrival-rate", type=float, default=d.late_arrival_rate)
    p.add_argument("--missing-key-rate", type=float, default=d.missing_key_rate)
    p.add_argument("--truncation-rate", type=float, default=d.truncation_rate)
    p.add_argument("--fuel-jump-rate", type=float, default=d.fuel_jump_rate)
    p.add_argument("--difficulty", choices=DIFFICULTIES, default=d.difficulty)
    p.add_argument("--start", default=d.start, help="first scan date, YYYY-MM-DD")
    p.add_argument("--span-days", type=int, default=d.span_days)
    a = p.parse_args(argv)
    return GenerationConfig(
        rows=a.rows,
        seed=a.seed,
        chunk_rows=a.chunk_rows,
        output_dir=a.output_dir,
        duplicate_rate=a.duplicate_rate,
        late_arrival_rate=a.late_arrival_rate,
        missing_key_rate=a.missing_key_rate,
        truncation_rate=a.truncation_rate,
        fuel_jump_rate=a.fuel_jump_rate,
        gzip_output=a.gzip,
        difficulty=a.difficulty,
        start=a.start,
        span_days=a.span_days,
    )


def main(argv=None) -> None:
    manifest = generate(parse_args(argv), progress=True)
    print(
        f"Wrote {manifest['total_rows']:,} rows in {manifest['elapsed_seconds']}s; "
        f"defects: {manifest['defects_injected']}"
    )


if __name__ == "__main__":
    main()
