"""Violation detectors and weekly reconciliation, all in Polars."""

from __future__ import annotations

import logging

import polars as pl

log = logging.getLogger(__name__)

# Funding vs invoice weekly totals within a cent are treated as reconciled.
RECONCILE_TOLERANCE = 0.01


def detect_duplicate_batches(claims: pl.DataFrame) -> pl.DataFrame:
    """Batches that appear in more than one funding report file.

    The overpaid estimate counts every payment beyond the batch's first
    (earliest-week) file — i.e. the duplicated portion.
    """
    per_file = (
        claims.group_by("batch_id", "source_file", "week")
        .agg(
            pl.len().alias("rows"),
            pl.col("total_payment_amount").sum().alias("payment"),
        )
        .sort("week", "source_file")
    )
    out = (
        per_file.group_by("batch_id")
        .agg(
            pl.col("source_file").alias("files"),
            pl.col("week").alias("weeks"),
            pl.col("source_file").n_unique().alias("n_files"),
            pl.col("rows").sum().alias("total_rows"),
            pl.col("payment").sum().alias("total_payment"),
            (pl.col("payment").sum() - pl.col("payment").first()).alias("overpaid"),
        )
        .filter(pl.col("n_files") > 1)
        .sort("overpaid", descending=True)
    )
    log.info("duplicate batches: %d found, %s estimated overpaid",
             out.height, f"{out['overpaid'].sum() or 0.0:,.2f}")
    for r in out.iter_rows(named=True):
        log.debug("  batch %s: %d files across weeks %s, overpaid %s (files: %s)",
                  r["batch_id"], r["n_files"], r["weeks"],
                  f"{r['overpaid']:,.2f}", r["files"])
    return out


def detect_duplicate_claims(claims: pl.DataFrame) -> pl.DataFrame:
    """Claim numbers submitted under more than one batch id."""
    out = (
        claims.group_by("claim_number")
        .agg(
            pl.col("batch_id").n_unique().alias("n_batches"),
            pl.col("batch_id").unique().sort().alias("batch_ids"),
            pl.col("total_payment_amount").sum().alias("total_payment"),
            pl.col("source_file").unique().sort().alias("files"),
            pl.col("week").unique().sort().alias("weeks"),
            pl.len().alias("occurrences"),
        )
        .filter(pl.col("n_batches") > 1)
        .sort("total_payment", descending=True)
    )
    log.info("duplicate claims: %d found", out.height)
    for r in out.iter_rows(named=True):
        log.debug("  claim %s: %d occurrences under batches %s in weeks %s, total payment %s",
                  r["claim_number"], r["occurrences"], r["batch_ids"], r["weeks"],
                  f"{r['total_payment']:,.2f}")
    return out


def duplicate_repayments(claims: pl.DataFrame) -> pl.DataFrame:
    """Dollars re-paid per week because of duplicates, attributed to the week
    of the re-occurrence (the first payment is the legitimate one).

    - dup_batch_repaid: full payment of every duplicated batch's second and
      later files.
    - dup_claim_repaid: payment of every second and later occurrence of a
      claim submitted under more than one batch — excluding rows that sit in
      a duplicated batch's re-submitted file, which are already counted above.
    """
    schema = {"week": pl.Utf8, "dup_batch_repaid": pl.Float64, "dup_claim_repaid": pl.Float64}
    if claims.height == 0:
        return pl.DataFrame(schema=schema)

    per_file = (
        claims.group_by("batch_id", "source_file", "week")
        .agg(pl.col("total_payment_amount").sum().alias("payment"))
        .sort("week", "source_file")
        .with_columns(pl.int_range(pl.len()).over("batch_id").alias("occ"))
    )
    dup_files = per_file.filter(pl.col("occ") > 0)
    batch_weekly = dup_files.group_by("week").agg(
        pl.col("payment").sum().alias("dup_batch_repaid")
    )

    claim_repays = (
        claims.sort("week", "source_file")
        .with_columns(
            pl.col("batch_id").n_unique().over("claim_number").alias("n_batches"),
            pl.int_range(pl.len()).over("claim_number").alias("occ"),
        )
        .filter((pl.col("n_batches") > 1) & (pl.col("occ") > 0))
        .join(dup_files.select("batch_id", "source_file"), on=["batch_id", "source_file"], how="anti")
    )
    claim_weekly = claim_repays.group_by("week").agg(
        pl.col("total_payment_amount").sum().alias("dup_claim_repaid")
    )

    if not batch_weekly.height and not claim_weekly.height:
        log.info("duplicate repayments: none")
        return pl.DataFrame(schema=schema)
    out = (
        batch_weekly.join(claim_weekly, on="week", how="full", coalesce=True)
        .with_columns(
            pl.col("dup_batch_repaid").fill_null(0.0),
            pl.col("dup_claim_repaid").fill_null(0.0),
        )
        .sort("week")
    )
    log.info(
        "duplicate repayments: %d weeks affected, %s from batches + %s from claims",
        out.height,
        f"{out['dup_batch_repaid'].sum():,.2f}", f"{out['dup_claim_repaid'].sum():,.2f}",
    )
    for r in out.iter_rows(named=True):
        log.debug("  week %s: batch repayments %s, claim repayments %s",
                  r["week"], f"{r['dup_batch_repaid']:,.2f}", f"{r['dup_claim_repaid']:,.2f}")
    return out


def weekly_reconciliation(claims: pl.DataFrame, invoices: pl.DataFrame) -> pl.DataFrame:
    """Per-week funding totals vs invoice totals, with the difference and the
    real overpayment.

    delta only measures disagreement between the week's own documents; the
    invoice includes any duplicated amounts, so duplicates never show up in
    it. overpayment adds them back: delta + duplicate repayments, i.e. what
    was paid beyond what the week legitimately owed. For weeks missing an
    invoice (null delta) it counts the duplicate repayments alone.
    """
    funding_weekly = claims.group_by("week").agg(
        pl.col("total_payment_amount").sum().alias("funding_total"),
        pl.col("gross_billable_charge").sum().alias("billed_total"),
        pl.col("batch_id").n_unique().alias("batches"),
        pl.len().alias("claim_rows"),
        pl.col("source_file").n_unique().alias("funding_files"),
    )
    out = (
        funding_weekly.join(
            invoices.select(
                "week", "invoice_number", "total_claims", "carrier_fees",
                "total_amount_due", "negative_batch",
            ),
            on="week",
            how="full",
            coalesce=True,
        )
        .join(duplicate_repayments(claims), on="week", how="left")
        .with_columns(
            pl.col("dup_batch_repaid").fill_null(0.0),
            pl.col("dup_claim_repaid").fill_null(0.0),
            (pl.col("funding_total") - pl.col("total_claims")).alias("delta"),
            (
                (pl.col("funding_total") - pl.col("total_claims")).abs()
                > RECONCILE_TOLERANCE
            )
            .fill_null(True)
            .alias("mismatch"),
        )
        .with_columns(
            (
                (pl.col("funding_total") - pl.col("total_claims")).fill_null(0.0)
                + pl.col("dup_batch_repaid")
                + pl.col("dup_claim_repaid")
            ).alias("overpayment")
        )
        .sort("week")
    )
    mismatched = out.filter(pl.col("mismatch"))
    log.info(
        "weekly reconciliation: %d weeks, %d mismatched, total real overpayment %s",
        out.height, mismatched.height, f"{out['overpayment'].sum() or 0.0:,.2f}",
    )
    for r in mismatched.iter_rows(named=True):
        log.debug(
            "  week %s MISMATCH: funding=%s invoice=%s delta=%s",
            r["week"],
            f"{r['funding_total']:,.2f}" if r["funding_total"] is not None else "—",
            f"{r['total_claims']:,.2f}" if r["total_claims"] is not None else "—",
            f"{r['delta']:,.2f}" if r["delta"] is not None else "—",
        )
    return out


def detect_line_anomalies(claims: pl.DataFrame) -> pl.DataFrame:
    """Row-level oddities: missing keys, non-positive amounts, payment > billed."""
    out = claims.with_columns(
        pl.when(pl.col("batch_id").is_null() | pl.col("claim_number").is_null())
        .then(pl.lit("missing identifier"))
        .when(pl.col("total_payment_amount") <= 0)
        .then(pl.lit("non-positive payment"))
        .when(pl.col("total_payment_amount") > pl.col("gross_billable_charge"))
        .then(pl.lit("payment exceeds billed charge"))
        .otherwise(pl.lit(None))
        .alias("anomaly")
    ).filter(pl.col("anomaly").is_not_null())
    by_type = {r["anomaly"]: r["n"] for r in
               out.group_by("anomaly").agg(pl.len().alias("n")).iter_rows(named=True)}
    log.info("line anomalies: %d found%s", out.height,
             f" ({', '.join(f'{k}: {v}' for k, v in sorted(by_type.items()))})" if by_type else "")
    for r in out.iter_rows(named=True):
        log.debug("  week %s %s: claim=%s batch=%s (%s)",
                  r["week"], r["source_file"], r["claim_number"], r["batch_id"], r["anomaly"])
    return out


def batch_summaries(claims: pl.DataFrame) -> pl.DataFrame:
    """One row per batch, for the explorer view."""
    out = (
        claims.group_by("batch_id")
        .agg(
            pl.col("week").unique().sort().alias("weeks"),
            pl.col("source_file").unique().sort().alias("files"),
            pl.len().alias("rows"),
            pl.col("total_payment_amount").sum().alias("total_payment"),
        )
        .sort("batch_id")
    )
    log.info("batch summaries: %d batches, %s total payment",
             out.height, f"{out['total_payment'].sum() or 0.0:,.2f}")
    return out
