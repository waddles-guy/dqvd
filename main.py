"""Data Quality Violations Detector — CLI entry point.

Usage:
    python main.py --input-path ./data --output-path ./output
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dqvd.parsing import load_directory
from dqvd.report import render_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect data quality violations in weekly billing data.")
    parser.add_argument("--input-path", type=Path, default=Path("data"), help="Folder of weekly YYYYMMDD subfolders")
    parser.add_argument("--output-path", type=Path, default=Path("output"), help="Where to write report.html")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if not args.input_path.is_dir():
        logging.error("Input path does not exist or is not a directory: %s", args.input_path)
        return 1

    claims, invoices = load_directory(args.input_path)
    report_file = render_report(claims, invoices, args.output_path)
    logging.info("Report written to %s", report_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
