"""Ingestion pipeline (PRD §5.1): parse -> zero-sum integrity gate -> post.

A batch that fails the zero-sum check is QUARANTINED: nothing is posted,
finance gets an exception. Idempotency is enforced at two levels —
file (batch_id) and record (psp:record_id) — so re-ingesting never
double-books; within-file duplicates are flagged, not posted twice.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from .canonical import PAYMENT, REFUND, SettlementBatch, zero_sum_check
from .ledger.ledger import CREDIT, DEBIT, JournalLine, Ledger, PSP_CASH_ACCOUNT
from .parsers.base import BaseParser


def ingest_file(
    ledger: Ledger, parser: BaseParser, path: str | Path, config: dict
) -> dict:
    path = Path(path)
    batch: SettlementBatch = parser.parse(path)
    file_key = f"file:{batch.batch_id}"
    if ledger.already_ingested(file_key):
        return {"status": "skipped", "batch": batch.batch_id, "posted": 0}

    violations = zero_sum_check(batch)
    if violations:
        for v in violations:
            ledger.add_exception(
                "BATCH_QUARANTINED",
                psp=batch.psp,
                detail=v,
            )
        ledger.audit("engine", "quarantine", f"{batch.batch_id}: {violations}")
        ledger.db.commit()
        return {"status": "quarantined", "batch": batch.batch_id, "violations": violations}

    posted, dupes, fee_flags = 0, 0, 0
    seen_in_file: set[str] = set()
    rate = config["psps"][batch.psp]["contracted_fee_rate"]
    for rec in batch.detail_records:
        rkey = f"rec:{rec.psp}:{rec.record_id}"
        if rec.record_id in seen_in_file or ledger.already_ingested(rkey):
            ledger.add_exception(
                "POSSIBLE_DUPLICATE_CAPTURE",
                record_id=rec.record_id,
                psp=rec.psp,
                merchant_reference=rec.merchant_reference,
                currency=rec.currency,
                amount_minor=rec.gross_amount,
                detail=f"duplicate of an already-ingested record in {batch.batch_id}",
            )
            dupes += 1
            continue
        seen_in_file.add(rec.record_id)

        # Fee validation (IXOPAY-style interchange check), payments only
        if rec.record_type == PAYMENT:
            expected_fee = round(abs(rec.gross_amount) * rate)
            if abs(rec.fee_amount) > expected_fee * 1.5 + 5:
                ledger.add_exception(
                    "FEE_VARIANCE",
                    record_id=rec.record_id,
                    psp=rec.psp,
                    merchant_reference=rec.merchant_reference,
                    currency=rec.currency,
                    amount_minor=rec.fee_amount,
                    detail=f"fee {rec.fee_amount} vs contracted {rate:.1%} "
                    f"(expected ~{expected_fee}) on gross {rec.gross_amount}",
                    suggestion="dispute with PSP / verify rate card",
                )
                fee_flags += 1

        # Post: Dr Cash_psp net / Dr Fees fee / Cr Suspense gross.
        # Signed amounts handle refunds (negative) generically.
        n = ledger.post_journal(
            [
                JournalLine(PSP_CASH_ACCOUNT[rec.psp], DEBIT, rec.net_amount, rec.currency),
                JournalLine("5000", DEBIT, rec.fee_amount, rec.currency),
                JournalLine("1200", CREDIT, rec.gross_amount, rec.currency),
            ],
            reference=f"settle:{rec.record_id}",
            batch_id=batch.batch_id,
            idempotency_key=rkey,
            txn_date=rec.settlement_date,
            description=f"{rec.psp} {rec.record_type} {rec.merchant_reference or rec.psp_reference}",
        )
        posted += 1 if n else 0
        cols = ["record_id", "psp", "batch_id", "record_type", "merchant_reference",
                "psp_reference", "gross_amount", "fee_amount", "net_amount",
                "currency", "settlement_date"]
        ledger.db.execute(
            ledger.db.insert_ignore("settlement_records", cols, "record_id, psp"),
            (
                rec.record_id, rec.psp, batch.batch_id, rec.record_type,
                rec.merchant_reference, rec.psp_reference, rec.gross_amount,
                rec.fee_amount, rec.net_amount, rec.currency,
                rec.settlement_date.isoformat(),
            ),
        )
    ledger.db.execute(
        "INSERT INTO ingestion_log (idempotency_key, source, ingested_at, journal_count)"
        " VALUES (?, ?, ?, ?)",
        (file_key, batch.batch_id, datetime.utcnow().isoformat(timespec="seconds"), posted),
    )
    ledger.audit(
        "engine", "ingest",
        f"{batch.batch_id}: {posted} posted, {dupes} duplicates flagged, {fee_flags} fee flags",
    )
    ledger.db.commit()
    return {
        "status": "ingested",
        "batch": batch.batch_id,
        "posted": posted,
        "duplicates": dupes,
        "fee_flags": fee_flags,
    }


def ingest_orders(ledger: Ledger, orders: list[dict]) -> int:
    """Post authorizations: Dr Cash-in-transit / Cr Revenue per order."""
    n = 0
    for o in orders:
        key = f"order:{o['merchant_reference']}"
        if ledger.already_ingested(key):
            continue
        ledger.post_journal(
            [
                JournalLine("1100", DEBIT, o["amount_minor"], o["currency"]),
                JournalLine("4000", CREDIT, o["amount_minor"], o["currency"]),
            ],
            reference=f"auth:{o['merchant_reference']}",
            batch_id="orders",
            idempotency_key=key,
            txn_date=date.fromisoformat(o["auth_date"]),
            description=f"authorized {o['merchant_reference']} via {o['psp']}",
        )
        cols = ["merchant_reference", "amount_minor", "currency", "auth_date", "psp"]
        ledger.db.execute(
            ledger.db.insert_ignore("orders", cols, "merchant_reference"),
            (o["merchant_reference"], o["amount_minor"], o["currency"],
             o["auth_date"], o["psp"]),
        )
        n += 1
    ledger.db.commit()
    return n
