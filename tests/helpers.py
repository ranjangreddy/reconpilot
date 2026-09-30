"""Shared fixtures for ReconPilot tests: in-memory ledger, tiny hand-built batches."""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from reconpilot.ledger.backend import SQLiteBackend
from reconpilot.ledger.ledger import Ledger
from reconpilot.matching.scorer import MatchScorer

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load(open(ROOT / "config.yaml"))

ADYEN_COLS = ["Company Account", "Merchant Account", "Psp Reference",
              "Merchant Reference", "Payment Method", "Creation Date", "TimeZone",
              "Type", "Modification Reference", "Record Type", "Gross Currency",
              "Gross (NC)", "Net Currency", "Net (NC)", "Commission (NC)",
              "Markup (NC)", "Interchange (NC)", "Scheme Fees (NC)",
              "Exchange Rate", "Batch Number", "Batch Closed Date"]


def make_ledger() -> Ledger:
    return Ledger(SQLiteBackend(":memory:"))


def make_scorer(tmp: str) -> MatchScorer:
    s = MatchScorer(Path(tmp) / "model.pkl")
    s.bootstrap()
    return s


def adyen_row(psp_ref: str, merchant_ref: str, gross: int, fee: int,
              day: str, record_type: str = "Settled") -> dict:
    comm, mark, inter = int(fee * 0.4), int(fee * 0.2), int(fee * 0.3)
    return {
        "Company Account": "TestCo", "Merchant Account": "TestMerchant",
        "Psp Reference": psp_ref, "Merchant Reference": merchant_ref,
        "Payment Method": "visa", "Creation Date": day, "TimeZone": "Europe/London",
        "Type": "Payment", "Modification Reference": "", "Record Type": record_type,
        "Gross Currency": "GBP", "Gross (NC)": gross,
        "Net Currency": "GBP", "Net (NC)": gross - fee,
        "Commission (NC)": comm, "Markup (NC)": mark,
        "Interchange (NC)": inter,
        "Scheme Fees (NC)": fee - comm - mark - inter,
        "Exchange Rate": "1.0", "Batch Number": f"adyen:{day}",
        "Batch Closed Date": day,
    }


def adyen_payout_row(day: str, net: int) -> dict:
    r = adyen_row(f"PAYOUT-{day}", "", 0, 0, day, "PaidOut")
    r["Net Currency"] = "GBP"
    r["Net (NC)"] = net
    return r


def write_csv(path: Path | str, cols: list[str], rows: list[dict]) -> None:
    import csv
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def journal_count(ledger: Ledger) -> int:
    cur = ledger.db.execute("SELECT COUNT(*) FROM journal_entries")
    return cur.fetchone()[0]
