"""Assemble the standalone HTML report.

All computed results are serialized to JSON and injected into the template,
which contains the inline CSS/JS that renders tables, charts, and the batch
explorer without a server.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import polars as pl

from . import violations as V


def _round(v: float | None, digits: int = 2) -> float | None:
    # adding 0.0 normalizes -0.0 to 0.0 so zero deltas never render with a minus sign
    return round(v, digits) + 0.0 if v is not None else None


def build_payload(claims: pl.DataFrame, invoices: pl.DataFrame) -> dict:
    dup_batches = V.detect_duplicate_batches(claims)
    dup_claims = V.detect_duplicate_claims(claims)
    weekly = V.weekly_reconciliation(claims, invoices)
    anomalies = V.detect_line_anomalies(claims)
    batches = V.batch_summaries(claims)

    mismatched_weeks = int(weekly.filter(pl.col("mismatch")).height)
    total_files = claims["source_file"].n_unique() + invoices["source_file"].n_unique()

    claims_by_batch: dict[str, list] = {}
    for row in claims.sort("week", "source_file", "claim_number").iter_rows(named=True):
        claims_by_batch.setdefault(row["batch_id"], []).append([
            row["claim_number"],
            str(row["service_date"]) if row["service_date"] else None,
            _round(row["gross_billable_charge"]),
            _round(row["total_payment_amount"]),
            row["plan_type"],
            row["provider"],
            row["week"],
            row["source_file"],
        ])

    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "summary": {
            "total_violations": dup_batches.height + dup_claims.height,
            "duplicate_batches": dup_batches.height,
            "duplicate_claims": dup_claims.height,
            "mismatched_weeks": mismatched_weeks,
            "weeks": weekly.height,
            "batches": int(claims["batch_id"].n_unique()),
            "files": int(total_files),
            "claim_rows": claims.height,
            "overpaid": _round(float(dup_batches["overpaid"].sum()) if dup_batches.height else 0.0),
            "anomalies": anomalies.height,
        },
        "duplicate_batches": [
            {
                "batch_id": r["batch_id"],
                "n_files": r["n_files"],
                "total_rows": r["total_rows"],
                "total_payment": _round(r["total_payment"]),
                "overpaid": _round(r["overpaid"]),
                "files": r["files"],
                "weeks": r["weeks"],
            }
            for r in dup_batches.iter_rows(named=True)
        ],
        "duplicate_claims": [
            {
                "claim_number": r["claim_number"],
                "n_batches": r["n_batches"],
                "batch_ids": r["batch_ids"],
                "occurrences": r["occurrences"],
                "total_payment": _round(r["total_payment"]),
                "files": r["files"],
                "weeks": r["weeks"],
            }
            for r in dup_claims.iter_rows(named=True)
        ],
        "weekly": [
            {
                "week": r["week"],
                "funding_files": r["funding_files"],
                "batches": r["batches"],
                "claim_rows": r["claim_rows"],
                "funding_total": _round(r["funding_total"]),
                "invoice_total": _round(r["total_claims"]),
                "invoice_due": _round(r["total_amount_due"]),
                "delta": _round(r["delta"]),
                "mismatch": bool(r["mismatch"]),
            }
            for r in weekly.iter_rows(named=True)
        ],
        "anomalies": [
            {
                "batch_id": r["batch_id"],
                "claim_number": r["claim_number"],
                "anomaly": r["anomaly"],
                "billed": _round(r["gross_billable_charge"]),
                "paid": _round(r["total_payment_amount"]),
                "week": r["week"],
                "source_file": r["source_file"],
            }
            for r in anomalies.iter_rows(named=True)
        ],
        "batches": [
            {
                "batch_id": r["batch_id"],
                "rows": r["rows"],
                "total_payment": _round(r["total_payment"]),
                "weeks": r["weeks"],
                "files": r["files"],
            }
            for r in batches.iter_rows(named=True)
        ],
        "claims_by_batch": claims_by_batch,
    }


def render_report(claims: pl.DataFrame, invoices: pl.DataFrame, output_path: Path) -> Path:
    payload = build_payload(claims, invoices)
    template = (
        resources.files("dqvd").joinpath("templates/report.html").read_text(encoding="utf-8")
    )
    html = template.replace(
        "/*__DATA__*/", "const DATA = " + json.dumps(payload, separators=(",", ":")) + ";"
    )
    output_path.mkdir(parents=True, exist_ok=True)
    report_file = output_path / "report.html"
    report_file.write_text(html, encoding="utf-8")
    return report_file
