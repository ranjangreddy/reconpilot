"""Shift4 settlement-statement parser.

Built to Shift4's public Settlement Statement spec (v1.5 rev 3): statement
fields include transaction_amount, funds_status, payment_currency, and the
merchant discount / buy-rate / rev-share / decline fee breakdown.

Deliberate design note: the Shift4 spec carries NO merchant order reference —
only Shift4's own payment_id. Those records therefore cannot match on a join
key and must clear through the tolerant + ML tiers. That is intentional: it
exercises the whole matching stack the way a real multi-PSP merchant's
worst-format file would.
"""
from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
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

FUNDS_STATUS_MAP = {
    "settled": PAYMENT,
    "refunded": REFUND,
    "chargeback": CHARGEBACK,
    "paid_out": PAYOUT,
}

FEE_COLUMNS = [
    "merchant_discount_fee",
    "buy_rate_fee",
    "rev_share_fee",
    "decline_fee",
]


def _minor(v: str) -> int:
    return int((Decimal(v or "0") * 100).to_integral_value())


class Shift4Parser(BaseParser):
    psp = "shift4"

    def parse(self, path: str | Path) -> SettlementBatch:
        path = Path(path)
        batch_id = f"shift4:{path.stem}"
        records: list[CanonicalRecord] = []
        payout_total: dict[str, int] = {}
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                record_type = FUNDS_STATUS_MAP.get(
                    row["funds_status"].strip(), ADJUSTMENT
                )
                currency = row["payment_currency"].strip().upper()
                gross = _minor(row["transaction_amount"])
                fee = sum(_minor(row[c]) for c in FEE_COLUMNS)
                net = gross - fee
                if record_type in (REFUND, CHARGEBACK):
                    gross, fee, net = -abs(gross), -abs(fee), -abs(net)
                if record_type == PAYOUT:
                    payout_total[currency] = payout_total.get(currency, 0) + net
                    continue
                records.append(
                    CanonicalRecord(
                        settlement_date=datetime.strptime(
                            row["payment_date"].strip(), "%Y-%m-%d"
                        ).date(),
                        record_id=row["payment_id"].strip(),
                        record_type=record_type,
                        merchant_reference="",  # not in the Shift4 spec
                        psp_reference=row["payment_id"].strip(),
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
