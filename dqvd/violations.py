"""Violation detectors and weekly reconciliation, all in Polars."""

from __future__ import annotations

import polars as pl

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
    return (
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


def detect_duplicate_claims(claims: pl.DataFrame) -> pl.DataFrame:
    """Claim numbers submitted under more than one batch id."""
    return (
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


def weekly_reconciliation(claims: pl.DataFrame, invoices: pl.DataFrame) -> pl.DataFrame:
    """Per-week funding totals vs invoice totals, with the difference."""
    funding_weekly = claims.group_by("week").agg(
        pl.col("total_payment_amount").sum().alias("funding_total"),
        pl.col("gross_billable_charge").sum().alias("billed_total"),
        pl.col("batch_id").n_unique().alias("batches"),
        pl.len().alias("claim_rows"),
        pl.col("source_file").n_unique().alias("funding_files"),
    )
    return (
        funding_weekly.join(
            invoices.select(
                "week", "invoice_number", "total_claims", "carrier_fees",
                "total_amount_due", "negative_batch",
            ),
            on="week",
            how="full",
            coalesce=True,
        )
        .with_columns(
            (pl.col("funding_total") - pl.col("total_claims")).alias("delta"),
            (
                (pl.col("funding_total") - pl.col("total_claims")).abs()
                > RECONCILE_TOLERANCE
            )
            .fill_null(True)
            .alias("mismatch"),
        )
        .sort("week")
    )


def detect_line_anomalies(claims: pl.DataFrame) -> pl.DataFrame:
    """Row-level oddities: missing keys, non-positive amounts, payment > billed."""
    return claims.with_columns(
        pl.when(pl.col("batch_id").is_null() | pl.col("claim_number").is_null())
        .then(pl.lit("missing identifier"))
        .when(pl.col("total_payment_amount") <= 0)
        .then(pl.lit("non-positive payment"))
        .when(pl.col("total_payment_amount") > pl.col("gross_billable_charge"))
        .then(pl.lit("payment exceeds billed charge"))
        .otherwise(pl.lit(None))
        .alias("anomaly")
    ).filter(pl.col("anomaly").is_not_null())


def batch_summaries(claims: pl.DataFrame) -> pl.DataFrame:
    """One row per batch, for the explorer view."""
    return (
        claims.group_by("batch_id")
        .agg(
            pl.col("week").unique().sort().alias("weeks"),
            pl.col("source_file").unique().sort().alias("files"),
            pl.len().alias("rows"),
            pl.col("total_payment_amount").sum().alias("total_payment"),
        )
        .sort("batch_id")
    )
