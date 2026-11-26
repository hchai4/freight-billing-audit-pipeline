"""Generate the stress datasets, audit them, and report time, memory and recall.

Runs three audits so the numbers can be read against each other:

* easy data  + reference rules  -- baseline; must be ~100%, proves the harness works
* hard data  + reference rules  -- same rules, messier data: what the easy score hid
* hard data  + blind detector   -- rules learned from the data, no generator constants

Each stage runs in its own subprocess so its peak memory (ru_maxrss from
``wait4``) is that stage alone, not the parent's high-water mark.

    python -m data.stress_benchmark                       # 2 x 2M rows per level
    python -m data.stress_benchmark --rows 100000         # quick smoke run
    python -m data.stress_benchmark --difficulty hard
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# (difficulty, detector) in the order they are reported.
AUDITS = (("easy", "reference"), ("hard", "reference"), ("hard", "blind"))


def run_stage(args: list) -> dict:
    """Run ``python -m <module> ...`` and return wall time, peak RSS and exit code."""
    started = time.perf_counter()
    proc = subprocess.Popen([sys.executable, "-m", *args], cwd=REPO_ROOT)
    _, status, usage = os.wait4(proc.pid, 0)
    elapsed = time.perf_counter() - started
    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    peak = usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss * 1024
    return {
        "wall_seconds": round(elapsed, 2),
        "peak_memory_mb": round(peak / 1024**2, 1),
        "exit_code": os.waitstatus_to_exitcode(status),
    }


def size_mb(paths) -> float:
    return round(sum(Path(p).stat().st_size for p in paths) / 1024**2, 1)


def generate_level(difficulty: str, args, out: Path) -> dict:
    cmd = ["data.stress_generator", "--rows", str(args.rows), "--seed", str(args.seed)]
    cmd += ["--chunk-rows", str(args.chunk_rows), "--output-dir", str(out)]
    cmd += ["--difficulty", difficulty] + (["--gzip"] if args.gzip else [])
    stage = run_stage(cmd)
    if not stage["exit_code"]:
        files = json.loads((out / "manifest.json").read_text())["files"]
        stage["rows_per_second"] = round(2 * args.rows / stage["wall_seconds"])
        stage["output_mb"] = size_mb(files.values())
    return stage


def audit_level(out: Path, detector: str, args) -> dict:
    report = out / f"audit_{detector}.json"
    cmd = ["data.stress_detectors", "--input-dir", str(out), "--detector", detector]
    cmd += ["--chunk-rows", str(args.chunk_rows), "--output", str(report)]
    stage = run_stage(cmd)
    if stage["exit_code"]:
        return stage
    quality = json.loads(report.read_text())
    return {**stage, "rows_per_second": quality["rows_per_second"], "quality": quality}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--rows", type=int, default=2_000_000, help="rows per table")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--chunk-rows", type=int, default=250_000)
    p.add_argument("--output-dir", type=Path, default=Path("data/stress_output"))
    p.add_argument("--difficulty", choices=("easy", "hard", "both"), default="both")
    p.add_argument("--gzip", action="store_true")
    args = p.parse_args(argv)

    levels = ("easy", "hard") if args.difficulty == "both" else (args.difficulty,)
    report = {
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "rows_per_table": args.rows,
        "total_rows": 2 * args.rows,
        "chunk_rows": args.chunk_rows,
        "generate": {},
        "audit": {},
    }
    for level in levels:
        out = args.output_dir / level
        print(f"[{level}] generating {2 * args.rows:,} rows ...")
        stage = generate_level(level, args, out)
        if stage["exit_code"]:
            print(f"generator failed ({level})", file=sys.stderr)
            return stage["exit_code"]
        report["generate"][level] = stage
        for audit_level_name, detector in AUDITS:
            if audit_level_name != level:
                continue
            print(f"[{level}] auditing with {detector} rules ...")
            result = audit_level(out, detector, args)
            if result["exit_code"]:
                print(f"audit failed ({level}/{detector})", file=sys.stderr)
                return result["exit_code"]
            report["audit"][f"{level}/{detector}"] = result

    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / "benchmark_report.json"
    path.write_text(json.dumps(report, indent=2))
    print_summary(report)
    print(f"\nFull report: {path}")
    return 0


def pct(value) -> str:
    return "  n/a" if value is None else f"{value * 100:5.1f}%"


def print_summary(r: dict) -> None:
    print(
        f"\n{r['total_rows']:,} rows per level | {r['machine']['platform']} | "
        f"Python {r['machine']['python']} | chunk {r['chunk_rows']:,}"
    )
    print(f"\n{'stage':<24}{'time (s)':>10}{'peak mem (MB)':>16}{'rows/s':>12}")
    for level, s in r["generate"].items():
        print(
            f"{'generate ' + level:<24}{s['wall_seconds']:>10}"
            f"{s['peak_memory_mb']:>16}{s['rows_per_second']:>12,}"
        )
    for name, s in r["audit"].items():
        print(
            f"{'audit ' + name:<24}{s['wall_seconds']:>10}"
            f"{s['peak_memory_mb']:>16}{s['rows_per_second']:>12,}"
        )

    for name, s in r["audit"].items():
        q = s["quality"]
        print(
            f"\n== {name}: recall {pct(q['overall_recall'])}, "
            f"precision {pct(q['overall_precision'])}"
        )
        print(
            f"{'defect':<20}{'injected':>10}{'flagged':>10}{'recall':>9}{'precision':>11}"
        )
        for issue, v in q["per_issue"].items():
            print(
                f"{issue:<20}{v['injected']:>10,}{v['flagged']:>10,}"
                f"{pct(v['recall']):>9}{pct(v['precision']):>11}"
            )
        weak = {k: v for k, v in q["per_variant"].items() if v["recall"] != 1}
        if weak:
            print("  variants below 100% recall:")
            for key, v in weak.items():
                print(
                    f"    {key:<34}{pct(v['recall'])}  ({v['caught']:,}/{v['injected']:,})"
                )
        for decoy, v in q["decoy_false_positives"].items():
            print(
                f"  {decoy}: {v['wrongly_flagged']:,} of {v['present']:,} wrongly flagged"
            )


if __name__ == "__main__":
    sys.exit(main())
