"""ReconPilot — canonical settlement schema.

Every PSP parser normalizes into CanonicalRecord. Amounts are integers in
minor units (cents/pence). Refunds/chargebacks carry NEGATIVE amounts so the
zero-sum invariant is a plain sum: every cent in = paid out, refunded, or
reserved (per the canonical settlement-file model).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

# Canonical record types (subset of the GitBook canonical model)
PAYMENT = "payment"
REFUND = "refund"
CHARGEBACK = "chargeback"
PAYOUT = "payout"        # summary row: what the PSP actually paid out
ADJUSTMENT = "adjustment"
FEE = "fee"

DETAIL_TYPES = {PAYMENT, REFUND, CHARGEBACK, ADJUSTMENT, FEE}


@dataclass
class CanonicalRecord:
    settlement_date: date
    record_id: str          # PSP's line-item id
    record_type: str        # one of the types above
    merchant_reference: str # OUR order/invoice ref — the primary join key
    psp_reference: str      # PSP's own reference for the payment
    gross_amount: int       # minor units, signed
    fee_amount: int         # minor units, signed
    net_amount: int         # minor units, signed
    currency: str           # ISO 4217
    psp: str                # adyen | stripe | shift4
    batch_id: str           # file / batch identifier
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # The money identity: net == gross - fee, always. A parser that cannot
        # satisfy this is misreading the PSP's format.
        if self.record_type in DETAIL_TYPES:
            assert self.net_amount == self.gross_amount - self.fee_amount, (
                f"net != gross - fee for {self.record_id}: "
                f"{self.net_amount} != {self.gross_amount} - {self.fee_amount}"
            )


@dataclass
class SettlementBatch:
    psp: str
    batch_id: str
    records: list[CanonicalRecord]
    payout_total: dict[str, int]  # currency -> minor units, from the PaidOut row(s)

    @property
    def detail_records(self) -> list[CanonicalRecord]:
        return [r for r in self.records if r.record_type in DETAIL_TYPES]


def zero_sum_check(batch: SettlementBatch) -> list[str]:
    """Integrity gate (PRD §7): per batch and currency, the sum of detail nets
    must equal the declared payout. Returns a list of violation strings;
    empty means the batch is clean. A violating batch is quarantined —
    never posted to the ledger."""
    violations: list[str] = []
    nets: dict[str, int] = {}
    for r in batch.detail_records:
        nets[r.currency] = nets.get(r.currency, 0) + r.net_amount
    for ccy, expected in batch.payout_total.items():
        actual = nets.get(ccy, 0)
        if actual != expected:
            violations.append(
                f"{batch.batch_id} {ccy}: detail nets sum to {actual}, "
                f"payout declares {expected} (diff {actual - expected})"
            )
    for ccy in nets:
        if ccy not in batch.payout_total:
            violations.append(
                f"{batch.batch_id} {ccy}: {nets[ccy]} of detail nets with no payout row"
            )
    return violations
