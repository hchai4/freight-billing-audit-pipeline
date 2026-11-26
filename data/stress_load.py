"""Load the generated stress CSVs into a throwaway Postgres for the dbt stress test.

Creates ``source_enterprise.raw_*`` tables with the columns the dbt sources expect
(no primary keys or NOT NULL, so defective rows load instead of failing) and a
``stress_eval.defect_labels`` table for scoring.

Phases, so a full dbt run and an incremental run can be timed separately:

    python -m data.stress_load                 # initial: first 90% of each table
    python -m data.stress_load --phase delta   # append the remaining 10%
    python -m data.stress_load --phase all     # everything at once

Rows are loaded in file order, and ``package_id`` / ``edi_record_id`` increase with
file order, so the delta rows are exactly the ids above the initial high-water mark.

Safety: this TRUNCATES ``source_enterprise.raw_*``. It refuses port 5432 (the
project's real database) unless ``--allow-main-db`` is passed.
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from pathlib import Path
from typing import IO, Iterator, List, Tuple
from urllib.parse import urlparse

import psycopg2

from data.stress_generator import EDI_COLUMNS, LABEL_COLUMNS, WMS_COLUMNS

DEFAULT_URL = "postgresql://postgres:postgres@localhost:5433/logistics_db"
MAIN_DB_PORT = 5432
COPY_BUFFER = 1 << 20

DDL = """
CREATE SCHEMA IF NOT EXISTS source_enterprise;
CREATE SCHEMA IF NOT EXISTS stress_eval;

CREATE TABLE IF NOT EXISTS source_enterprise.raw_wms_package_scans (
    package_id VARCHAR(64), tracking_id VARCHAR(64),
    carrier_code VARCHAR(16), service_level VARCHAR(32),
    actual_scale_weight NUMERIC(8, 2),
    package_length_in NUMERIC(8, 2), package_width_in NUMERIC(8, 2),
    package_height_in NUMERIC(8, 2),
    origin_warehouse VARCHAR(32), origin_zip VARCHAR(16), dest_zip VARCHAR(16),
    dest_address_type VARCHAR(16), dest_address_cass_validated BOOLEAN,
    scan_timestamp TIMESTAMP, dock_departure_timestamp TIMESTAMP,
    expected_cost NUMERIC(10, 2),
    ingestion_timestamp TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_enterprise.raw_edi_ups_invoices (
    edi_record_id VARCHAR(64), tracking_id VARCHAR(64),
    carrier_invoice_id VARCHAR(64), billed_weight NUMERIC(8, 2),
    base_rate NUMERIC(10, 2), fuel_surcharge NUMERIC(10, 2),
    total_billed_amount NUMERIC(10, 2),
    service_level_billed VARCHAR(32), zone_billed INTEGER, dest_zip VARCHAR(16),
    delivery_timestamp TIMESTAMP, invoice_date DATE
);

-- The dbt project also reads these two; the stress run leaves them empty.
CREATE TABLE IF NOT EXISTS source_enterprise.raw_portal_extracted_invoices (
    portal_record_id INTEGER, tracking_id VARCHAR(64), invoice_number VARCHAR(64),
    carrier_name VARCHAR(32), billed_weight NUMERIC(8, 2), base_charge NUMERIC(10, 2),
    fuel_surcharge NUMERIC(10, 2), grand_total NUMERIC(10, 2),
    service_level_billed VARCHAR(32), zone_billed INTEGER, dest_zip VARCHAR(16),
    delivery_timestamp TIMESTAMP, invoice_date DATE,
    extraction_timestamp TIMESTAMP, extraction_source VARCHAR(32)
);
CREATE TABLE IF NOT EXISTS source_enterprise.raw_carrier_accessorial_charges (
    accessorial_charge_id VARCHAR(64), tracking_id VARCHAR(64),
    invoice_number VARCHAR(64), source_channel VARCHAR(32),
    charge_line_number INTEGER, accessorial_code VARCHAR(48),
    accessorial_amount NUMERIC(10, 2), invoice_date DATE
);

-- row_id is the 0-based file row; record_id is the id used in the raw tables.
CREATE TABLE IF NOT EXISTS stress_eval.defect_labels (
    dataset TEXT, row_id BIGINT, issue_type TEXT, related_row_id BIGINT,
    variant TEXT,
    record_id TEXT GENERATED ALWAYS AS (
        CASE WHEN dataset = 'edi' THEN 'EDI-' ELSE 'PKG-' END
        || lpad(row_id::text, 8, '0')
    ) STORED
);
"""

TABLES = {
    "wms": ("source_enterprise.raw_wms_package_scans", WMS_COLUMNS),
    "edi": ("source_enterprise.raw_edi_ups_invoices", EDI_COLUMNS),
}
LABELS_TABLE = "stress_eval.defect_labels"
EMPTY_TABLES = (
    "source_enterprise.raw_portal_extracted_invoices",
    "source_enterprise.raw_carrier_accessorial_charges",
)


class LineStream:
    """File-like object over an iterator of text lines, for ``copy_expert``."""

    def __init__(self, lines: Iterator[str]) -> None:
        self._lines = lines
        self._buffer = ""

    def read(self, size: int = -1) -> str:
        while size < 0 or len(self._buffer) < size:
            line = next(self._lines, None)
            if line is None:
                break
            self._buffer += line
        if size < 0:
            out, self._buffer = self._buffer, ""
        else:
            out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out


def split_point(rows: int, fraction: float) -> int:
    if not 0 < fraction < 1:
        raise ValueError("fraction must be between 0 and 1")
    return int(rows * fraction)


def row_range(phase: str, rows: int, fraction: float) -> Tuple[int, int]:
    """Half-open range of data-row indices a phase loads."""
    if phase == "all":
        return 0, rows
    cut = split_point(rows, fraction)
    return (0, cut) if phase == "initial" else (cut, rows)


def _open_text(path: Path) -> IO[str]:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", newline="")
    return open(path, "r", newline="")


def _rows(path: Path, first: int, stop: int) -> Iterator[str]:
    with _open_text(path) as handle:
        next(handle)  # header
        for index, line in enumerate(handle):
            if index >= stop:
                return
            if index >= first:
                yield line


def copy_rows(cur, table: str, columns: List[str], path: Path, first: int, stop: int):
    sql = (
        f"COPY {table} ({', '.join(columns)}) "
        "FROM STDIN WITH (FORMAT csv, HEADER false)"
    )
    cur.copy_expert(sql, LineStream(_rows(path, first, stop)), COPY_BUFFER)


def load(url: str, input_dir: Path, phase: str, fraction: float) -> dict:
    manifest = json.loads((input_dir / "manifest.json").read_text())
    files = {k: Path(v) for k, v in manifest["files"].items()}
    first, stop = row_range(phase, manifest["rows_per_table"], fraction)
    report = {"phase": phase, "rows_per_table": stop - first, "seconds": {}}

    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute(DDL)
        if phase != "delta":
            cur.execute(
                "TRUNCATE "
                + ", ".join([t for t, _ in TABLES.values()] + [LABELS_TABLE])
                + ", "
                + ", ".join(EMPTY_TABLES)
            )
        else:
            for dataset, (table, _) in TABLES.items():
                cur.execute(f"SELECT count(*) FROM {table}")
                if cur.fetchone()[0] != first:
                    raise SystemExit(
                        f"{table} must hold exactly {first:,} rows before the "
                        "delta load; run the initial phase first."
                    )
        for dataset, (table, columns) in TABLES.items():
            started = time.perf_counter()
            copy_rows(cur, table, columns, files[dataset], first, stop)
            report["seconds"][dataset] = round(time.perf_counter() - started, 1)
        if phase != "delta":
            started = time.perf_counter()
            copy_rows(cur, LABELS_TABLE, LABEL_COLUMNS, files["labels"], 0, 10**12)
            report["seconds"]["labels"] = round(time.perf_counter() - started, 1)
        conn.commit()
        started = time.perf_counter()
        for table, _ in TABLES.values():
            cur.execute(f"ANALYZE {table}")
        cur.execute(f"ANALYZE {LABELS_TABLE}")
        conn.commit()
        report["seconds"]["analyze"] = round(time.perf_counter() - started, 1)
        counts = {}
        for dataset, (table, _) in TABLES.items():
            cur.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FROM {LABELS_TABLE}")
        counts[LABELS_TABLE] = cur.fetchone()[0]
    report["table_rows"] = counts
    return report


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--input-dir", type=Path, default=Path("data/stress_output/dbt"))
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--phase", choices=("initial", "delta", "all"), default="initial")
    p.add_argument("--fraction", type=float, default=0.9)
    p.add_argument("--allow-main-db", action="store_true")
    args = p.parse_args(argv)

    if urlparse(args.url).port == MAIN_DB_PORT and not args.allow_main_db:
        raise SystemExit(
            f"Refusing to load into port {MAIN_DB_PORT}: that is the project's real "
            "database. Use the stress container on 5433, or pass --allow-main-db."
        )
    print(
        json.dumps(load(args.url, args.input_dir, args.phase, args.fraction), indent=2)
    )


if __name__ == "__main__":
    main()
