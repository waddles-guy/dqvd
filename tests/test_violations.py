"""Tests for the violation detectors and weekly reconciliation in dqvd/violations.py.

Run with:  python -m unittest discover tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl

from dqvd import violations as V


def claims_df(rows: list[dict]) -> pl.DataFrame:
    defaults = {
        "batch_id": "B1",
        "claim_number": "C1",
        "service_date": None,
        "gross_billable_charge": 100.0,
        "total_payment_amount": 80.0,
        "plan_type": "PPO",
        "provider": "Prov",
        "week": "20260713",
        "source_file": "funding_1.xlsx",
    }
    schema = {
        "batch_id": pl.Utf8, "claim_number": pl.Utf8, "service_date": pl.Utf8,
        "gross_billable_charge": pl.Float64, "total_payment_amount": pl.Float64,
        "plan_type": pl.Utf8, "provider": pl.Utf8, "week": pl.Utf8, "source_file": pl.Utf8,
    }
    return pl.DataFrame([{**defaults, **r} for r in rows], schema=schema)


def invoices_df(rows: list[dict]) -> pl.DataFrame:
    defaults = {
        "week": "20260713",
        "invoice_number": "INV1",
        "total_claims": 80.0,
        "carrier_fees": 0.0,
        "total_amount_due": 80.0,
        "negative_batch": False,
        "source_file": "invoice_1.xlsx",
    }
    schema = {
        "week": pl.Utf8, "invoice_number": pl.Utf8, "total_claims": pl.Float64,
        "carrier_fees": pl.Float64, "total_amount_due": pl.Float64,
        "negative_batch": pl.Boolean, "source_file": pl.Utf8,
    }
    return pl.DataFrame([{**defaults, **r} for r in rows], schema=schema)


class DuplicateBatchTests(unittest.TestCase):
    def test_no_duplicates_returns_empty(self):
        claims = claims_df([
            {"batch_id": "B1", "source_file": "f1.xlsx"},
            {"batch_id": "B2", "source_file": "f2.xlsx"},
        ])
        self.assertEqual(V.detect_duplicate_batches(claims).height, 0)

    def test_same_batch_in_two_files_is_flagged(self):
        claims = claims_df([
            {"batch_id": "B1", "source_file": "f1.xlsx", "total_payment_amount": 100.0},
            {"batch_id": "B1", "source_file": "f2.xlsx", "total_payment_amount": 100.0},
        ])
        out = V.detect_duplicate_batches(claims)
        self.assertEqual(out.height, 1)
        row = out.row(0, named=True)
        self.assertEqual(row["n_files"], 2)
        self.assertEqual(row["total_rows"], 2)
        self.assertEqual(row["total_payment"], 200.0)
        self.assertEqual(row["overpaid"], 100.0)
        self.assertEqual(sorted(row["files"]), ["f1.xlsx", "f2.xlsx"])

    def test_multiple_rows_in_one_file_is_not_a_duplicate(self):
        claims = claims_df([
            {"batch_id": "B1", "claim_number": "C1"},
            {"batch_id": "B1", "claim_number": "C2"},
        ])
        self.assertEqual(V.detect_duplicate_batches(claims).height, 0)

    def test_overpaid_counts_everything_beyond_earliest_week_file(self):
        # First (earliest-week) file paid 100; later files paid 60 + 40.
        claims = claims_df([
            {"batch_id": "B1", "week": "20260706", "source_file": "f1.xlsx", "total_payment_amount": 100.0},
            {"batch_id": "B1", "week": "20260713", "source_file": "f2.xlsx", "total_payment_amount": 60.0},
            {"batch_id": "B1", "week": "20260720", "source_file": "f3.xlsx", "total_payment_amount": 40.0},
        ])
        row = V.detect_duplicate_batches(claims).row(0, named=True)
        self.assertEqual(row["n_files"], 3)
        self.assertEqual(row["total_payment"], 200.0)
        self.assertEqual(row["overpaid"], 100.0)  # 200 - first file's 100
        self.assertEqual(row["weeks"][0], "20260706")  # earliest week is the baseline

    def test_sorted_by_overpaid_descending(self):
        claims = claims_df([
            {"batch_id": "SMALL", "source_file": "f1.xlsx", "total_payment_amount": 10.0},
            {"batch_id": "SMALL", "source_file": "f2.xlsx", "week": "20260720", "total_payment_amount": 10.0},
            {"batch_id": "BIG", "source_file": "f1.xlsx", "total_payment_amount": 500.0},
            {"batch_id": "BIG", "source_file": "f2.xlsx", "week": "20260720", "total_payment_amount": 500.0},
        ])
        out = V.detect_duplicate_batches(claims)
        self.assertEqual(out["batch_id"].to_list(), ["BIG", "SMALL"])
        self.assertEqual(out["overpaid"].to_list(), [500.0, 10.0])

    def test_duplicate_within_same_week_different_files(self):
        claims = claims_df([
            {"batch_id": "B1", "source_file": "f1.xlsx"},
            {"batch_id": "B1", "source_file": "f2.xlsx"},
        ])
        self.assertEqual(V.detect_duplicate_batches(claims).height, 1)


class DuplicateClaimTests(unittest.TestCase):
    def test_claim_in_single_batch_not_flagged(self):
        claims = claims_df([
            {"claim_number": "C1", "batch_id": "B1"},
            {"claim_number": "C2", "batch_id": "B2"},
        ])
        self.assertEqual(V.detect_duplicate_claims(claims).height, 0)

    def test_claim_repeated_within_same_batch_not_flagged(self):
        claims = claims_df([
            {"claim_number": "C1", "batch_id": "B1"},
            {"claim_number": "C1", "batch_id": "B1"},
        ])
        self.assertEqual(V.detect_duplicate_claims(claims).height, 0)

    def test_claim_in_two_batches_is_flagged(self):
        claims = claims_df([
            {"claim_number": "C1", "batch_id": "B2", "total_payment_amount": 30.0,
             "source_file": "f2.xlsx", "week": "20260720"},
            {"claim_number": "C1", "batch_id": "B1", "total_payment_amount": 50.0,
             "source_file": "f1.xlsx", "week": "20260713"},
        ])
        out = V.detect_duplicate_claims(claims)
        self.assertEqual(out.height, 1)
        row = out.row(0, named=True)
        self.assertEqual(row["n_batches"], 2)
        self.assertEqual(row["batch_ids"], ["B1", "B2"])  # sorted
        self.assertEqual(row["occurrences"], 2)
        self.assertEqual(row["total_payment"], 80.0)
        self.assertEqual(row["files"], ["f1.xlsx", "f2.xlsx"])  # unique sorted
        self.assertEqual(row["weeks"], ["20260713", "20260720"])

    def test_sorted_by_total_payment_descending(self):
        claims = claims_df([
            {"claim_number": "CHEAP", "batch_id": "B1", "total_payment_amount": 1.0},
            {"claim_number": "CHEAP", "batch_id": "B2", "total_payment_amount": 1.0},
            {"claim_number": "PRICEY", "batch_id": "B1", "total_payment_amount": 900.0},
            {"claim_number": "PRICEY", "batch_id": "B2", "total_payment_amount": 900.0},
        ])
        out = V.detect_duplicate_claims(claims)
        self.assertEqual(out["claim_number"].to_list(), ["PRICEY", "CHEAP"])


class WeeklyReconciliationTests(unittest.TestCase):
    def week_row(self, out: pl.DataFrame, week: str) -> dict:
        return out.filter(pl.col("week") == week).row(0, named=True)

    def test_matching_totals_reconcile(self):
        out = V.weekly_reconciliation(
            claims_df([{"total_payment_amount": 80.0}]),
            invoices_df([{"total_claims": 80.0}]),
        )
        row = self.week_row(out, "20260713")
        self.assertFalse(row["mismatch"])
        self.assertEqual(row["delta"], 0.0)

    def test_funding_total_sums_all_rows_and_files(self):
        out = V.weekly_reconciliation(
            claims_df([
                {"claim_number": "C1", "batch_id": "B1", "source_file": "f1.xlsx",
                 "total_payment_amount": 10.0, "gross_billable_charge": 20.0},
                {"claim_number": "C2", "batch_id": "B1", "source_file": "f1.xlsx",
                 "total_payment_amount": 15.0, "gross_billable_charge": 25.0},
                {"claim_number": "C3", "batch_id": "B2", "source_file": "f2.xlsx",
                 "total_payment_amount": 5.0, "gross_billable_charge": 5.0},
            ]),
            invoices_df([{"total_claims": 30.0}]),
        )
        row = self.week_row(out, "20260713")
        self.assertEqual(row["funding_total"], 30.0)
        self.assertEqual(row["billed_total"], 50.0)
        self.assertEqual(row["batches"], 2)
        self.assertEqual(row["claim_rows"], 3)
        self.assertEqual(row["funding_files"], 2)
        self.assertFalse(row["mismatch"])

    def test_difference_above_tolerance_is_mismatch(self):
        out = V.weekly_reconciliation(
            claims_df([{"total_payment_amount": 80.0}]),
            invoices_df([{"total_claims": 90.0}]),
        )
        row = self.week_row(out, "20260713")
        self.assertTrue(row["mismatch"])
        self.assertEqual(row["delta"], -10.0)

    def test_overpayment_is_also_mismatch(self):
        out = V.weekly_reconciliation(
            claims_df([{"total_payment_amount": 100.0}]),
            invoices_df([{"total_claims": 90.0}]),
        )
        row = self.week_row(out, "20260713")
        self.assertTrue(row["mismatch"])
        self.assertEqual(row["delta"], 10.0)

    def test_difference_of_exactly_one_cent_reconciles(self):
        # tolerance is strict: mismatch only when |delta| > 0.01
        out = V.weekly_reconciliation(
            claims_df([{"total_payment_amount": 0.01}]),
            invoices_df([{"total_claims": 0.0}]),
        )
        self.assertFalse(self.week_row(out, "20260713")["mismatch"])

    def test_sub_cent_difference_reconciles(self):
        out = V.weekly_reconciliation(
            claims_df([{"total_payment_amount": 80.005}]),
            invoices_df([{"total_claims": 80.0}]),
        )
        self.assertFalse(self.week_row(out, "20260713")["mismatch"])

    def test_week_with_claims_but_no_invoice_is_mismatch_with_null_delta(self):
        out = V.weekly_reconciliation(
            claims_df([{"week": "20260713"}]),
            invoices_df([{"week": "20260720"}]),
        )
        row = self.week_row(out, "20260713")
        self.assertIsNone(row["delta"])
        self.assertTrue(row["mismatch"])

    def test_week_with_invoice_but_no_claims_is_mismatch_with_null_totals(self):
        out = V.weekly_reconciliation(
            claims_df([{"week": "20260713"}]),
            invoices_df([{"week": "20260720"}]),
        )
        row = self.week_row(out, "20260720")
        self.assertIsNone(row["funding_total"])
        self.assertIsNone(row["delta"])
        self.assertTrue(row["mismatch"])

    def test_output_sorted_by_week(self):
        out = V.weekly_reconciliation(
            claims_df([{"week": "20260720"}, {"week": "20260706"}, {"week": "20260713"}]),
            invoices_df([]),
        )
        self.assertEqual(out["week"].to_list(), ["20260706", "20260713", "20260720"])

    def test_one_output_row_per_week(self):
        out = V.weekly_reconciliation(
            claims_df([
                {"week": "20260713", "claim_number": "C1"},
                {"week": "20260713", "claim_number": "C2"},
            ]),
            invoices_df([{"week": "20260713"}]),
        )
        self.assertEqual(out.height, 1)


class DuplicateRepaymentTests(unittest.TestCase):
    def repayments(self, rows: list[dict]) -> dict[str, tuple[float, float]]:
        out = V.duplicate_repayments(claims_df(rows))
        return {
            r["week"]: (r["dup_batch_repaid"], r["dup_claim_repaid"])
            for r in out.iter_rows(named=True)
        }

    def test_no_duplicates_yields_empty(self):
        self.assertEqual(self.repayments([
            {"batch_id": "B1", "claim_number": "C1"},
            {"batch_id": "B2", "claim_number": "C2"},
        ]), {})

    def test_empty_claims_yields_empty_typed_frame(self):
        out = V.duplicate_repayments(claims_df([]).clear())
        self.assertEqual(out.height, 0)
        self.assertIn("dup_batch_repaid", out.columns)

    def test_batch_repayment_attributed_to_reoccurrence_week_only(self):
        rep = self.repayments([
            {"batch_id": "B1", "week": "20260706", "source_file": "f1.xlsx", "total_payment_amount": 100.0},
            {"batch_id": "B1", "week": "20260713", "source_file": "f2.xlsx", "total_payment_amount": 100.0},
        ])
        self.assertNotIn("20260706", rep)  # first payment is legitimate
        self.assertEqual(rep["20260713"], (100.0, 0.0))

    def test_third_occurrence_also_counted_in_its_own_week(self):
        rep = self.repayments([
            {"batch_id": "B1", "week": "20260706", "source_file": "f1.xlsx", "total_payment_amount": 100.0},
            {"batch_id": "B1", "week": "20260713", "source_file": "f2.xlsx", "total_payment_amount": 60.0},
            {"batch_id": "B1", "week": "20260720", "source_file": "f3.xlsx", "total_payment_amount": 40.0},
        ])
        self.assertEqual(rep["20260713"], (60.0, 0.0))
        self.assertEqual(rep["20260720"], (40.0, 0.0))

    def test_claim_repayment_attributed_to_later_week(self):
        rep = self.repayments([
            {"claim_number": "C1", "batch_id": "B1", "week": "20260706",
             "source_file": "f1.xlsx", "total_payment_amount": 50.0},
            {"claim_number": "C1", "batch_id": "B2", "week": "20260713",
             "source_file": "f2.xlsx", "total_payment_amount": 55.0},
        ])
        self.assertNotIn("20260706", rep)
        self.assertEqual(rep["20260713"], (0.0, 55.0))

    def test_claim_repeated_within_same_batch_not_counted(self):
        self.assertEqual(self.repayments([
            {"claim_number": "C1", "batch_id": "B1", "total_payment_amount": 50.0},
            {"claim_number": "C1", "batch_id": "B1", "total_payment_amount": 50.0},
        ]), {})

    def test_claim_inside_duplicated_batch_file_not_double_counted(self):
        # Batch B1 is re-submitted in week 2 (f2.xlsx). Claim C1 also appears
        # there under B1 after first appearing under B9 — its dollars are part
        # of the batch repayment and must not be counted again as a claim one.
        rep = self.repayments([
            {"claim_number": "C1", "batch_id": "B9", "week": "20260629",
             "source_file": "f0.xlsx", "total_payment_amount": 10.0},
            {"claim_number": "C2", "batch_id": "B1", "week": "20260706",
             "source_file": "f1.xlsx", "total_payment_amount": 100.0},
            {"claim_number": "C1", "batch_id": "B1", "week": "20260713",
             "source_file": "f2.xlsx", "total_payment_amount": 10.0},
            {"claim_number": "C2", "batch_id": "B1", "week": "20260713",
             "source_file": "f2.xlsx", "total_payment_amount": 100.0},
        ])
        self.assertEqual(rep["20260713"], (110.0, 0.0))  # batch only, no claim part

    def test_batch_and_claim_repayments_in_same_week_are_separate(self):
        rep = self.repayments([
            {"batch_id": "B1", "claim_number": "C1", "week": "20260706",
             "source_file": "f1.xlsx", "total_payment_amount": 100.0},
            {"batch_id": "B1", "claim_number": "C1b", "week": "20260713",
             "source_file": "f2.xlsx", "total_payment_amount": 100.0},
            {"claim_number": "C9", "batch_id": "B8", "week": "20260706",
             "source_file": "f1.xlsx", "total_payment_amount": 7.0},
            {"claim_number": "C9", "batch_id": "B7", "week": "20260713",
             "source_file": "f3.xlsx", "total_payment_amount": 7.0},
        ])
        self.assertEqual(rep["20260713"], (100.0, 7.0))


class WeeklyOverpaymentTests(unittest.TestCase):
    def week_row(self, claims_rows, invoices_rows, week="20260713"):
        out = V.weekly_reconciliation(claims_df(claims_rows), invoices_df(invoices_rows))
        return out.filter(pl.col("week") == week).row(0, named=True)

    def test_clean_week_has_zero_overpayment(self):
        row = self.week_row([{"total_payment_amount": 80.0}], [{"total_claims": 80.0}])
        self.assertEqual(row["overpayment"], 0.0)
        self.assertEqual(row["dup_batch_repaid"], 0.0)
        self.assertEqual(row["dup_claim_repaid"], 0.0)

    def test_overpayment_is_delta_plus_duplicates(self):
        # Week 2: invoice includes the re-submitted batch (100), funding also
        # paid 5 more than invoiced -> overpayment = 5 + 100.
        claims = [
            {"batch_id": "B1", "claim_number": "C1", "week": "20260706",
             "source_file": "f1.xlsx", "total_payment_amount": 100.0},
            {"batch_id": "B1", "claim_number": "C1b", "week": "20260713",
             "source_file": "f2.xlsx", "total_payment_amount": 100.0},
            {"batch_id": "B2", "claim_number": "C2", "week": "20260713",
             "source_file": "f3.xlsx", "total_payment_amount": 105.0},
        ]
        invoices = [
            {"week": "20260706", "total_claims": 100.0, "source_file": "i1.xlsx"},
            {"week": "20260713", "total_claims": 200.0, "source_file": "i2.xlsx",
             "invoice_number": "INV2"},
        ]
        row = self.week_row(claims, invoices)
        self.assertEqual(row["delta"], 5.0)
        self.assertEqual(row["dup_batch_repaid"], 100.0)
        self.assertEqual(row["overpayment"], 105.0)
        first = self.week_row(claims, invoices, week="20260706")
        self.assertEqual(first["overpayment"], 0.0)

    def test_duplicates_reconciled_week_still_shows_overpayment(self):
        # invoice matches funding exactly (duplicate included): mismatch is
        # False but the real overpayment is the duplicate amount.
        claims = [
            {"batch_id": "B1", "claim_number": "C1", "week": "20260706",
             "source_file": "f1.xlsx", "total_payment_amount": 100.0},
            {"batch_id": "B1", "claim_number": "C1b", "week": "20260713",
             "source_file": "f2.xlsx", "total_payment_amount": 100.0},
        ]
        invoices = [
            {"week": "20260706", "total_claims": 100.0, "source_file": "i1.xlsx"},
            {"week": "20260713", "total_claims": 100.0, "source_file": "i2.xlsx"},
        ]
        row = self.week_row(claims, invoices)
        self.assertFalse(row["mismatch"])
        self.assertEqual(row["overpayment"], 100.0)

    def test_negative_delta_offsets_duplicates_in_the_sum(self):
        claims = [
            {"batch_id": "B1", "claim_number": "C1", "week": "20260706",
             "source_file": "f1.xlsx", "total_payment_amount": 100.0},
            {"batch_id": "B1", "claim_number": "C1b", "week": "20260713",
             "source_file": "f2.xlsx", "total_payment_amount": 100.0},
        ]
        invoices = [
            {"week": "20260706", "total_claims": 100.0, "source_file": "i1.xlsx"},
            {"week": "20260713", "total_claims": 130.0, "source_file": "i2.xlsx"},
        ]
        row = self.week_row(claims, invoices)
        self.assertEqual(row["delta"], -30.0)
        self.assertEqual(row["overpayment"], 70.0)  # -30 + 100

    def test_missing_invoice_counts_duplicates_alone(self):
        claims = [
            {"batch_id": "B1", "claim_number": "C1", "week": "20260706",
             "source_file": "f1.xlsx", "total_payment_amount": 100.0},
            {"batch_id": "B1", "claim_number": "C1b", "week": "20260713",
             "source_file": "f2.xlsx", "total_payment_amount": 100.0},
        ]
        invoices = [{"week": "20260706", "total_claims": 100.0}]
        row = self.week_row(claims, invoices)
        self.assertIsNone(row["delta"])
        self.assertEqual(row["overpayment"], 100.0)


class LineAnomalyTests(unittest.TestCase):
    def anomalies(self, rows: list[dict]) -> list[str]:
        return V.detect_line_anomalies(claims_df(rows))["anomaly"].to_list()

    def test_clean_rows_produce_nothing(self):
        self.assertEqual(
            self.anomalies([{"gross_billable_charge": 100.0, "total_payment_amount": 80.0}]),
            [],
        )

    def test_payment_equal_to_billed_is_clean(self):
        self.assertEqual(
            self.anomalies([{"gross_billable_charge": 100.0, "total_payment_amount": 100.0}]),
            [],
        )

    def test_missing_batch_id(self):
        self.assertEqual(self.anomalies([{"batch_id": None}]), ["missing identifier"])

    def test_missing_claim_number(self):
        self.assertEqual(self.anomalies([{"claim_number": None}]), ["missing identifier"])

    def test_zero_payment(self):
        self.assertEqual(
            self.anomalies([{"total_payment_amount": 0.0}]), ["non-positive payment"]
        )

    def test_negative_payment(self):
        self.assertEqual(
            self.anomalies([{"total_payment_amount": -5.0}]), ["non-positive payment"]
        )

    def test_payment_exceeds_billed(self):
        self.assertEqual(
            self.anomalies([{"gross_billable_charge": 100.0, "total_payment_amount": 150.0}]),
            ["payment exceeds billed charge"],
        )

    def test_missing_identifier_takes_precedence_over_amount_checks(self):
        self.assertEqual(
            self.anomalies([{"batch_id": None, "total_payment_amount": -5.0}]),
            ["missing identifier"],
        )
        self.assertEqual(
            self.anomalies([
                {"claim_number": None, "gross_billable_charge": 10.0, "total_payment_amount": 50.0}
            ]),
            ["missing identifier"],
        )

    def test_each_bad_row_flagged_independently(self):
        out = self.anomalies([
            {"claim_number": "C1"},  # clean
            {"claim_number": "C2", "total_payment_amount": 0.0},
            {"claim_number": "C3", "batch_id": None},
            {"claim_number": "C4", "gross_billable_charge": 10.0, "total_payment_amount": 20.0},
        ])
        self.assertEqual(
            sorted(out),
            ["missing identifier", "non-positive payment", "payment exceeds billed charge"],
        )


class BatchSummaryTests(unittest.TestCase):
    def test_one_row_per_batch_sorted_by_id(self):
        claims = claims_df([
            {"batch_id": "B2"},
            {"batch_id": "B1", "claim_number": "C1"},
            {"batch_id": "B1", "claim_number": "C2"},
        ])
        out = V.batch_summaries(claims)
        self.assertEqual(out["batch_id"].to_list(), ["B1", "B2"])
        self.assertEqual(out["rows"].to_list(), [2, 1])

    def test_totals_weeks_and_files_aggregate(self):
        claims = claims_df([
            {"batch_id": "B1", "week": "20260720", "source_file": "f2.xlsx", "total_payment_amount": 10.0},
            {"batch_id": "B1", "week": "20260713", "source_file": "f1.xlsx", "total_payment_amount": 30.0},
            {"batch_id": "B1", "week": "20260713", "source_file": "f1.xlsx", "total_payment_amount": 5.0},
        ])
        row = V.batch_summaries(claims).row(0, named=True)
        self.assertEqual(row["total_payment"], 45.0)
        self.assertEqual(row["weeks"], ["20260713", "20260720"])  # unique sorted
        self.assertEqual(row["files"], ["f1.xlsx", "f2.xlsx"])


if __name__ == "__main__":
    unittest.main()
