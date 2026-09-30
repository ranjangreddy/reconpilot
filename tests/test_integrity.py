"""Integrity gate: a tampered batch is quarantined — nothing is posted."""
from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

from reconpilot import ingest as ingest_mod
from reconpilot.parsers import PARSERS
from tests.helpers import (
    ADYEN_COLS, CONFIG, adyen_payout_row, adyen_row, journal_count,
    make_ledger, write_csv,
)


class TestIntegrity(unittest.TestCase):
    def test_tampered_batch_quarantined(self):
        ledger = make_ledger()
        with TemporaryDirectory() as tmp:
            f = f"{tmp}/2011-01-04.csv"
            rows = [
                adyen_row("T00000000000001", "INV-1", 10000, 190, "2011-01-04"),
                adyen_row("T00000000000002", "INV-2", 20000, 380, "2011-01-04"),
                # tampered: declares 28,000 but details net to 29,430
                adyen_payout_row("2011-01-04", 28000),
            ]
            write_csv(f, ADYEN_COLS, rows)
            res = ingest_mod.ingest_file(ledger, PARSERS["adyen"](), f, CONFIG)
        self.assertEqual(res["status"], "quarantined")
        self.assertTrue(res["violations"])
        self.assertEqual(journal_count(ledger), 0)
        cur = ledger.db.execute(
            "SELECT COUNT(*) FROM settlement_records")
        self.assertEqual(cur.fetchone()[0], 0)
        cur = ledger.db.execute(
            "SELECT reason_code FROM exceptions")
        self.assertEqual(cur.fetchone()[0], "BATCH_QUARANTINED")

    def test_clean_batch_passes(self):
        ledger = make_ledger()
        with TemporaryDirectory() as tmp:
            f = f"{tmp}/2011-01-04.csv"
            rows = [
                adyen_row("T00000000000001", "INV-1", 10000, 190, "2011-01-04"),
                adyen_row("T00000000000002", "INV-2", 20000, 380, "2011-01-04"),
                adyen_payout_row("2011-01-04", 29430),
            ]
            write_csv(f, ADYEN_COLS, rows)
            res = ingest_mod.ingest_file(ledger, PARSERS["adyen"](), f, CONFIG)
        self.assertEqual(res["status"], "ingested")
        self.assertEqual(res["posted"], 2)
        self.assertGreater(journal_count(ledger), 0)


if __name__ == "__main__":
    unittest.main()
