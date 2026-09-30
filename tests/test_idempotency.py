"""Idempotency: re-ingesting a file or an order never double-books."""
from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

from reconpilot import ingest as ingest_mod
from reconpilot.parsers import PARSERS
from tests.helpers import (
    ADYEN_COLS, CONFIG, adyen_payout_row, adyen_row, journal_count,
    make_ledger, write_csv,
)

ORDERS = [
    {"merchant_reference": "INV-1", "amount_minor": 10000, "currency": "GBP",
     "auth_date": "2011-01-02", "psp": "adyen"},
    {"merchant_reference": "INV-2", "amount_minor": 20000, "currency": "GBP",
     "auth_date": "2011-01-02", "psp": "adyen"},
]

ROWS = [
    adyen_row("T00000000000001", "INV-1", 10000, 190, "2011-01-04"),
    adyen_row("T00000000000002", "INV-2", 20000, 380, "2011-01-04"),
    adyen_payout_row("2011-01-04", 29430),
]


class TestIdempotency(unittest.TestCase):
    def test_double_ingest_same_journal_count(self):
        ledger = make_ledger()
        with TemporaryDirectory() as tmp:
            f = f"{tmp}/2011-01-04.csv"
            write_csv(f, ADYEN_COLS, ROWS)
            r1 = ingest_mod.ingest_file(ledger, PARSERS["adyen"](), f, CONFIG)
            n1 = journal_count(ledger)
            r2 = ingest_mod.ingest_file(ledger, PARSERS["adyen"](), f, CONFIG)
            n2 = journal_count(ledger)
        self.assertEqual(r1["status"], "ingested")
        self.assertEqual(r2["status"], "skipped")
        self.assertEqual(n1, n2)
        self.assertGreater(n1, 0)

    def test_double_order_ingest_same_journal_count(self):
        ledger = make_ledger()
        n1_orders = ingest_mod.ingest_orders(ledger, ORDERS)
        n1 = journal_count(ledger)
        n2_orders = ingest_mod.ingest_orders(ledger, ORDERS)
        n2 = journal_count(ledger)
        self.assertEqual(n1_orders, 2)
        self.assertEqual(n2_orders, 0)
        self.assertEqual(n1, n2)


if __name__ == "__main__":
    unittest.main()
