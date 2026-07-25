"""Tests for report payload building — negative-zero delta normalization.

Run with:  python -m unittest discover tests -v
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl

from dqvd.report import _round, build_payload, render_report


class RoundTests(unittest.TestCase):
    def assert_positive_zero(self, value):
        self.assertEqual(value, 0.0)
        self.assertEqual(math.copysign(1.0, value), 1.0, f"{value!r} is negative zero")

    def test_none_passes_through(self):
        self.assertIsNone(_round(None))

    def test_positive_zero_stays_positive(self):
        self.assert_positive_zero(_round(0.0))

    def test_negative_zero_normalized(self):
        self.assert_positive_zero(_round(-0.0))

    def test_tiny_negative_rounds_to_positive_zero(self):
        # round(-1e-09, 2) == -0.0 in plain Python; _round must flip the sign bit
        self.assert_positive_zero(_round(-1e-09))
        self.assert_positive_zero(_round(-0.001))
        self.assert_positive_zero(_round(-0.004))

    def test_tiny_positive_rounds_to_zero(self):
        self.assert_positive_zero(_round(0.004))

    def test_real_negative_values_keep_sign(self):
        self.assertEqual(_round(-0.01), -0.01)
        self.assertEqual(_round(-3.14159), -3.14)
        self.assertEqual(_round(-1234.567), -1234.57)

    def test_positive_values_unchanged(self):
        self.assertEqual(_round(5.678), 5.68)
        self.assertEqual(_round(1234.5), 1234.5)

    def test_custom_digits(self):
        self.assert_positive_zero(_round(-0.00004, 4))
        self.assertEqual(_round(-0.00006, 4), -0.0001)
        self.assertEqual(_round(1.23456, 3), 1.235)

    def test_json_serialization_has_no_minus_zero(self):
        self.assertEqual(json.dumps(_round(-1e-09)), "0.0")
        self.assertEqual(json.dumps(_round(-0.0)), "0.0")


def _claims(rows: list[dict]) -> pl.DataFrame:
    defaults = {
        "batch_id": "B1",
        "claim_number": "C1",
        "service_date": None,
        "gross_billable_charge": 100.0,
        "total_payment_amount": 100.0,
        "plan_type": "PPO",
        "provider": "Prov",
        "week": "20260713",
        "source_file": "funding_1.xlsx",
    }
    return pl.DataFrame([{**defaults, **r} for r in rows])


def _invoices(rows: list[dict]) -> pl.DataFrame:
    defaults = {
        "week": "20260713",
        "invoice_number": "INV1",
        "total_claims": 100.0,
        "carrier_fees": 0.0,
        "total_amount_due": 100.0,
        "negative_batch": False,
        "source_file": "invoice_1.xlsx",
    }
    return pl.DataFrame([{**defaults, **r} for r in rows])


class PayloadDeltaTests(unittest.TestCase):
    def week_deltas(self, payload: dict) -> dict[str, float | None]:
        return {w["week"]: w["delta"] for w in payload["weekly"]}

    def test_exact_match_gives_positive_zero_delta(self):
        payload = build_payload(
            _claims([{"total_payment_amount": 100.0}]),
            _invoices([{"total_claims": 100.0}]),
        )
        delta = self.week_deltas(payload)["20260713"]
        self.assertEqual(delta, 0.0)
        self.assertEqual(math.copysign(1.0, delta), 1.0, "delta is -0.0")

    def test_float_noise_below_a_cent_gives_positive_zero_delta(self):
        # 0.1 + 0.2 style float noise: funding sums to slightly LESS than the
        # invoice total, so the raw delta is a tiny negative number.
        payload = build_payload(
            _claims([
                {"claim_number": "C1", "total_payment_amount": 0.1},
                {"claim_number": "C2", "total_payment_amount": 0.2},
            ]),
            _invoices([{"total_claims": 0.3, "total_amount_due": 0.3}]),
        )
        week = next(w for w in payload["weekly"] if w["week"] == "20260713")
        self.assertFalse(week["mismatch"])
        self.assertEqual(week["delta"], 0.0)
        self.assertEqual(math.copysign(1.0, week["delta"]), 1.0, "delta is -0.0")

    def test_real_negative_delta_survives(self):
        payload = build_payload(
            _claims([{"total_payment_amount": 90.0}]),
            _invoices([{"total_claims": 100.0}]),
        )
        self.assertEqual(self.week_deltas(payload)["20260713"], -10.0)

    def test_real_positive_delta_survives(self):
        payload = build_payload(
            _claims([{"total_payment_amount": 110.0}]),
            _invoices([{"total_claims": 100.0}]),
        )
        self.assertEqual(self.week_deltas(payload)["20260713"], 10.0)

    def test_week_missing_invoice_has_null_delta(self):
        payload = build_payload(
            _claims([{"total_payment_amount": 100.0}]),
            _invoices([{"week": "20260720", "source_file": "invoice_2.xlsx"}]),
        )
        deltas = self.week_deltas(payload)
        self.assertIsNone(deltas["20260713"])  # claims, no invoice
        self.assertIsNone(deltas["20260720"])  # invoice, no claims

    def test_rendered_html_contains_no_minus_zero_delta(self):
        claims = _claims([
            {"claim_number": "C1", "total_payment_amount": 0.1},
            {"claim_number": "C2", "total_payment_amount": 0.2},
        ])
        invoices = _invoices([{"total_claims": 0.3, "total_amount_due": 0.3}])
        with tempfile.TemporaryDirectory() as tmp:
            html = render_report(claims, invoices, Path(tmp)).read_text(encoding="utf-8")
        self.assertNotIn('"delta":-0.0', html)
        self.assertNotIn('"delta":-0,', html)


if __name__ == "__main__":
    unittest.main()
