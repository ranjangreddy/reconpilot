"""Stripe payout-reconciliation parser.

Built to Stripe's public payout reconciliation report spec
(https://docs.stripe.com/reports/report-types/payout-reconciliation):
gross/fee/net with payout linkage via automatic_payout_id. Stripe reports in
major units; we normalize to minor units. The reporting_category vocabulary
(charge/refund/dispute/payout) maps onto ledger postings.
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

CATEGORY_MAP = {
    "charge": PAYMENT,
    "refund": REFUND,
    "dispute": CHARGEBACK,
    "payout": PAYOUT,
}


def _minor(v: str) -> int:
    return int((Decimal(v or "0") * 100).to_integral_value())


class StripeParser(BaseParser):
    psp = "stripe"

    def parse(self, path: str | Path) -> SettlementBatch:
        path = Path(path)
        batch_id = f"stripe:{path.stem}"
        records: list[CanonicalRecord] = []
        payout_total: dict[str, int] = {}
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                record_type = CATEGORY_MAP.get(
                    row["reporting_category"].strip(), ADJUSTMENT
                )
                currency = row["currency"].strip().upper()
                gross = _minor(row["gross"])
                fee = _minor(row["fee"])
                net = _minor(row["net"])
                # Stripe signs refunds/disputes negative in the report already;
                # normalize sign by category to be safe.
                if record_type in (REFUND, CHARGEBACK):
                    gross, fee, net = -abs(gross), -abs(fee), -abs(net)
                if record_type == PAYOUT:
                    payout_total[currency] = payout_total.get(currency, 0) + net
                    continue
                # merchant_reference rides in the description ("Order INV-123")
                desc = row.get("description", "")
                merchant_reference = (
                    desc.replace("Order ", "").strip() if "Order " in desc else ""
                )
                # settlement_date: Stripe's real report scopes rows by the payout's
                # interval parameters; the per-file date here comes from the
                # daily file name (demo affordance, documented in the parser).
                sdate = row.get("settlement_date", "").strip() or path.stem
                records.append(
                    CanonicalRecord(
                        settlement_date=datetime.strptime(sdate, "%Y-%m-%d").date(),
                        record_id=row["balance_transaction_id"].strip(),
                        record_type=record_type,
                        merchant_reference=merchant_reference,
                        psp_reference=row["payment_intent_id"].strip()
                        or row["charge_id"].strip(),
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
