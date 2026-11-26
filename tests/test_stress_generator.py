import json

import pandas as pd
import pytest

from data.stress_detectors import run_audit
from data.stress_generator import (
    EDI_COLUMNS,
    ISSUE_TYPES,
    LABEL_COLUMNS,
    WMS_COLUMNS,
    GenerationConfig,
    generate,
)

ROWS = 40_000


def make(tmp_path, **overrides):
    cfg = GenerationConfig(
        rows=ROWS, chunk_rows=10_000, output_dir=tmp_path, **overrides
    )
    return cfg, generate(cfg)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("stress")
    cfg, manifest = make(out)
    return {
        "dir": out,
        "manifest": manifest,
        "wms": pd.read_csv(manifest["files"]["wms"], dtype=str, keep_default_na=False),
        "edi": pd.read_csv(manifest["files"]["edi"], dtype=str, keep_default_na=False),
        "labels": pd.read_csv(manifest["files"]["labels"]),
    }


def test_schema_and_row_counts(dataset):
    assert list(dataset["wms"].columns) == WMS_COLUMNS
    assert list(dataset["edi"].columns) == EDI_COLUMNS
    assert list(dataset["labels"].columns) == LABEL_COLUMNS
    assert len(dataset["wms"]) == len(dataset["edi"]) == ROWS
    assert dataset["manifest"]["total_rows"] == 2 * ROWS


def test_every_defect_type_is_injected_near_its_rate(dataset):
    counts = dataset["manifest"]["defects_injected"]
    expected = {
        "DUPLICATE_INVOICE": 0.03 * ROWS,
        "LATE_ARRIVAL": 0.02 * ROWS,
        "MISSING_KEY": 0.015 * ROWS,
        "TRUNCATED_FIELD": 0.02 * ROWS,
        "FUEL_TIER_JUMP": 0.005 * ROWS,
    }
    assert set(counts) == set(ISSUE_TYPES)
    for issue, target in expected.items():
        assert counts[issue] == pytest.approx(target, rel=0.25), issue


def test_edi_defects_never_share_a_row(dataset):
    edi_labels = dataset["labels"].query("dataset == 'edi'")
    assert not edi_labels["row_id"].duplicated().any()


def test_duplicates_repeat_an_earlier_tracking_and_invoice_pair(dataset):
    edi = dataset["edi"]
    dups = dataset["labels"].query("issue_type == 'DUPLICATE_INVOICE'")
    assert (dups["related_row_id"] < dups["row_id"]).all()
    for row_id, source in zip(dups["row_id"].head(200), dups["related_row_id"]):
        a, b = edi.iloc[row_id], edi.iloc[source]
        assert a["tracking_id"] == b["tracking_id"]
        assert a["carrier_invoice_id"] == b["carrier_invoice_id"]
        assert a["edi_record_id"] != b["edi_record_id"]


def test_late_arrivals_land_in_a_later_calendar_month(dataset):
    wms = dataset["wms"]
    late = dataset["labels"].query("issue_type == 'LATE_ARRIVAL'")["row_id"]
    scan = pd.to_datetime(wms["scan_timestamp"])
    arrived = pd.to_datetime(wms["ingestion_timestamp"])
    assert (
        arrived.iloc[late].dt.to_period("M") > scan.iloc[late].dt.to_period("M")
    ).all()
    normal = wms.index.difference(late)
    assert ((arrived - scan).iloc[normal] < pd.Timedelta(days=1)).all()


def test_clean_rows_have_consistent_money(dataset):
    edi = dataset["edi"]
    flagged = dataset["labels"].query("dataset == 'edi'")["row_id"]
    clean = edi.drop(index=flagged).astype(
        {"base_rate": float, "fuel_surcharge": float, "total_billed_amount": float}
    )
    gap = clean["base_rate"] + clean["fuel_surcharge"] - clean["total_billed_amount"]
    assert gap.abs().max() < 0.005
    assert (clean["tracking_id"].str.len() == 18).all()


def test_same_arguments_give_identical_files(tmp_path, dataset):
    _, again = make(tmp_path)
    for key in ("wms", "edi", "labels"):
        assert (
            open(again["files"][key]).read()
            == open(dataset["manifest"]["files"][key]).read()
        )


def test_different_seed_changes_the_data(tmp_path, dataset):
    _, other = make(tmp_path, seed=7)
    assert (
        open(other["files"]["edi"]).read()
        != open(dataset["manifest"]["files"]["edi"]).read()
    )


def test_reference_detectors_find_every_injected_defect(dataset):
    report = run_audit(dataset["dir"], chunk_rows=7_000)
    assert report["overall_recall"] == 1.0
    assert report["overall_precision"] == 1.0
    for issue in ISSUE_TYPES:
        assert report["per_issue"][issue]["injected"] > 0


def test_gzip_output_audits_the_same(tmp_path):
    cfg = GenerationConfig(
        rows=8_000, chunk_rows=3_000, output_dir=tmp_path, gzip_output=True
    )
    manifest = generate(cfg)
    assert manifest["files"]["edi"].endswith(".csv.gz")
    assert run_audit(tmp_path, chunk_rows=2_500)["overall_recall"] == 1.0


def test_rejects_impossible_rates(tmp_path):
    with pytest.raises(ValueError):
        generate(GenerationConfig(rows=10, output_dir=tmp_path, duplicate_rate=0.99))
    manifest = (
        json.loads((tmp_path / "manifest.json").read_text())
        if (tmp_path / "manifest.json").exists()
        else None
    )
    assert manifest is None


# ----------------------------------------------------------------- hard mode


@pytest.fixture(scope="module")
def hard(tmp_path_factory):
    out = tmp_path_factory.mktemp("stress_hard")
    cfg = GenerationConfig(
        rows=ROWS, chunk_rows=10_000, output_dir=out, difficulty="hard"
    )
    manifest = generate(cfg)
    return {
        "dir": out,
        "manifest": manifest,
        "wms": pd.read_csv(manifest["files"]["wms"], dtype=str, keep_default_na=False),
        "edi": pd.read_csv(manifest["files"]["edi"], dtype=str, keep_default_na=False),
        "labels": pd.read_csv(manifest["files"]["labels"], keep_default_na=False),
    }


def test_easy_labels_are_all_standard_with_no_decoys(dataset):
    assert set(dataset["labels"]["variant"]) == {"standard"}
    assert set(dataset["manifest"]["decoys_injected"].values()) == {0}


def test_unknown_difficulty_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        generate(GenerationConfig(rows=10, output_dir=tmp_path, difficulty="insane"))


def test_hard_mode_is_deterministic(tmp_path, hard):
    other = tmp_path / "again"
    generate(
        GenerationConfig(
            rows=ROWS, chunk_rows=10_000, output_dir=other, difficulty="hard"
        )
    )
    for name in ("ups_edi_invoices.csv", "wms_package_scans.csv", "defect_labels.csv"):
        assert (other / name).read_text() == (hard["dir"] / name).read_text()


def test_hard_mode_uses_every_variant(hard):
    variants = hard["labels"].query("issue_type in @ISSUE_TYPES")
    seen = set(variants["issue_type"] + "/" + variants["variant"])
    assert {
        "DUPLICATE_INVOICE/exact",
        "DUPLICATE_INVOICE/formatted",
        "LATE_ARRIVAL/cross_month",
        "LATE_ARRIVAL/days_late",
        "LATE_ARRIVAL/hours_late",
        "MISSING_KEY/blank",
        "MISSING_KEY/sentinel",
        "MISSING_KEY/placeholder",
        "TRUNCATED_FIELD/cut",
        "TRUNCATED_FIELD/padded",
        "FUEL_TIER_JUMP/major",
        "FUEL_TIER_JUMP/one_tier",
        "FUEL_TIER_JUMP/minor",
    } <= seen
    assert "standard" not in set(variants["variant"])


def test_hard_mode_stacks_defects_on_some_edi_rows(hard):
    edi_labels = hard["labels"].query("dataset == 'edi' and variant != 'legit'")
    stacked = int(edi_labels["row_id"].duplicated().sum())
    primary = 0.07 * ROWS  # duplicate + missing + truncation + fuel rates
    assert 0.05 * primary < stacked < 0.15 * primary


def test_decoys_are_counted_apart_from_defects(hard):
    decoys = hard["manifest"]["decoys_injected"]
    assert all(count > 0 for count in decoys.values())
    assert not set(decoys) & set(hard["manifest"]["defects_injected"])


def test_split_line_decoys_share_a_pair_but_not_an_amount(hard):
    edi = hard["edi"]
    split = hard["labels"].query("issue_type == 'DECOY_SPLIT_LINE'")
    assert len(split) > 0
    for row_id, origin in zip(split["row_id"].head(100), split["related_row_id"]):
        a, b = edi.iloc[row_id], edi.iloc[origin]
        assert a["tracking_id"] == b["tracking_id"]
        assert a["carrier_invoice_id"] == b["carrier_invoice_id"]
        assert a["total_billed_amount"] != b["total_billed_amount"]


def test_slow_batch_decoys_arrive_within_a_day(hard):
    wms = hard["wms"]
    slow = hard["labels"].query("issue_type == 'DECOY_SLOW_BATCH'")["row_id"]
    lag = pd.to_datetime(wms["ingestion_timestamp"]) - pd.to_datetime(
        wms["scan_timestamp"]
    )
    assert (lag.iloc[slow] >= pd.Timedelta(hours=6)).all()
    assert (lag.iloc[slow] < pd.Timedelta(hours=20)).all()


def test_blind_detector_beats_reference_rules_on_hard_data(hard):
    reference = run_audit(hard["dir"], chunk_rows=7_000, detector="reference")
    blind = run_audit(hard["dir"], chunk_rows=7_000, detector="blind")
    assert reference["overall_recall"] < 0.9  # easy-data rules miss the variants
    assert blind["overall_recall"] > reference["overall_recall"]
    assert blind["overall_precision"] > reference["overall_precision"]
    # ...but hard data is not solved: some variants are genuinely missed.
    assert blind["overall_recall"] < 1.0
    assert blind["per_variant"]["LATE_ARRIVAL/hours_late"]["recall"] < 1.0
    assert blind["per_variant"]["FUEL_TIER_JUMP/minor"]["recall"] < 1.0


def test_blind_detector_still_fooled_by_a_decoy(hard):
    blind = run_audit(hard["dir"], chunk_rows=7_000, detector="blind")
    grace = blind["decoy_false_positives"]["DECOY_GRACE_RATE"]
    assert grace["wrongly_flagged"] > 0
    assert blind["decoy_false_positives"]["DECOY_SLOW_BATCH"]["wrongly_flagged"] == 0


def test_reference_rules_are_still_perfect_on_easy_data(dataset):
    report = run_audit(dataset["dir"], chunk_rows=7_000, detector="reference")
    assert report["overall_recall"] == 1.0 and report["overall_precision"] == 1.0
