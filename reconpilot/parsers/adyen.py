"""Adyen settlement-details parser.

Built to Adyen's public settlement report spec
(https://docs.adyen.com/reporting/settlement-reconciliation/transaction-level/settlement-details-report):
the Merchant Reference <-> 16-char Psp Reference pair is the join key, and the
Record Type journal vocabulary (Settled/Refunded/Chargeback/PaidOut) maps 1:1
onto ledger postings. Amounts are in minor units, as in Adyen's reports.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from ..canonical import (
    ADJUSTMENT,
    CHARGEBACK,
    PAYOUT,
    PAYMENT,
    REFUND,
    CanonicalRecord,
    SettlementBatch,
)
from .base import BaseParser

RECORD_TYPE_MAP = {
    "Settled": PAYMENT,
    "Refunded": REFUND,
    "Chargeback": CHARGEBACK,
    "PaidOut": PAYOUT,
}

FEE_COLUMNS = ["Commission (NC)", "Markup (NC)", "Interchange (NC)", "Scheme Fees (NC)"]


def _int(v: str) -> int:
    return int(float(v or 0))


class AdyenParser(BaseParser):
    psp = "adyen"

    def parse(self, path: str | Path) -> SettlementBatch:
        path = Path(path)
        batch_id = f"adyen:{path.stem}"
        records: list[CanonicalRecord] = []
        payout_total: dict[str, int] = {}
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                record_type = RECORD_TYPE_MAP.get(row["Record Type"].strip(), ADJUSTMENT)
                currency = row["Net Currency"].strip()
                gross = _int(row["Gross (NC)"])
                fee = sum(_int(row[c]) for c in FEE_COLUMNS)
                net = _int(row["Net (NC)"])
                if record_type == PAYOUT:
                    payout_total[currency] = payout_total.get(currency, 0) + net
                    continue
                records.append(
                    CanonicalRecord(
                        settlement_date=datetime.strptime(
                            row["Batch Closed Date"].strip(), "%Y-%m-%d"
                        ).date(),
                        record_id=row["Psp Reference"].strip(),
                        record_type=record_type,
                        merchant_reference=row["Merchant Reference"].strip(),
                        psp_reference=row["Psp Reference"].strip(),
                        gross_amount=gross,
                        fee_amount=fee,
                        net_amount=net,
                        currency=currency,
                        psp=self.psp,
                        batch_id=batch_id,
                        raw=dict(row),
                    )
                )
        return SettlementBatch(
            psp=self.psp, batch_id=batch_id, records=records, payout_total=payout_total
        )
