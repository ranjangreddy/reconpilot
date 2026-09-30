"""ReconPilot double-entry ledger (PRD §5.2).

Schema shape modeled on Apache Fineract's acc_gl_journal_entry: DEBIT/CREDIT
enum, reference_number as the recon join key, period closure. Every movement
is a balanced journal entry; unmatched settlements park in suspense accounts;
idempotency keys on ingestion mean re-ingesting a file never double-books.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .backend import Backend, autoincrement

DEBIT = "DEBIT"
CREDIT = "CREDIT"

# Chart of accounts for the demo merchant
CHART = [
    ("1000", "Cash — Adyen settlement", "ASSET"),
    ("1010", "Cash — Stripe settlement", "ASSET"),
    ("1020", "Cash — Shift4 settlement", "ASSET"),
    ("1100", "Cash in transit (authorized, not yet settled)", "ASSET"),
    ("1200", "Suspense — unmatched settlements", "ASSET"),
    ("4000", "Revenue — card sales", "INCOME"),
    ("5000", "Expense — PSP fees", "EXPENSE"),
    ("5010", "Expense — FX variance", "EXPENSE"),
]

PSP_CASH_ACCOUNT = {"adyen": "1000", "stripe": "1010", "shift4": "1020"}


@dataclass
class JournalLine:
    account_code: str
    dc: str  # DEBIT or CREDIT
    amount_minor: int
    currency: str


class Ledger:
    def __init__(self, backend: Backend):
        self.db = backend
        self.init_schema()

    # ---- schema ----
    def init_schema(self) -> None:
        ai = autoincrement(self.db)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS accounts (
                code TEXT PRIMARY KEY, name TEXT NOT NULL, classification TEXT NOT NULL)"""
        )
        self.db.execute(
            f"""CREATE TABLE IF NOT EXISTS journal_entries (
                id {ai}, account_code TEXT NOT NULL, currency TEXT NOT NULL,
                transaction_date TEXT NOT NULL, entry_date TEXT NOT NULL,
                dc TEXT NOT NULL, amount_minor INTEGER NOT NULL,
                reference_number TEXT NOT NULL, description TEXT,
                batch_id TEXT, idempotency_key TEXT NOT NULL)"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS ingestion_log (
                idempotency_key TEXT PRIMARY KEY, source TEXT NOT NULL,
                ingested_at TEXT NOT NULL, journal_count INTEGER NOT NULL)"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS settlement_records (
                record_id TEXT NOT NULL, psp TEXT NOT NULL, batch_id TEXT NOT NULL,
                record_type TEXT NOT NULL, merchant_reference TEXT,
                psp_reference TEXT, gross_amount INTEGER NOT NULL,
                fee_amount INTEGER NOT NULL, net_amount INTEGER NOT NULL,
                currency TEXT NOT NULL, settlement_date TEXT NOT NULL,
                match_status TEXT NOT NULL DEFAULT 'unmatched',
                matched_order_ref TEXT, match_tier INTEGER, match_confidence REAL,
                match_reason TEXT, PRIMARY KEY (record_id, psp))"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                merchant_reference TEXT PRIMARY KEY, amount_minor INTEGER NOT NULL,
                currency TEXT NOT NULL, auth_date TEXT NOT NULL, psp TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open')"""
        )
        self.db.execute(
            f"""CREATE TABLE IF NOT EXISTS exceptions (
                id {ai}, reason_code TEXT NOT NULL, record_id TEXT,
                psp TEXT, merchant_reference TEXT, currency TEXT,
                amount_minor INTEGER, detail TEXT, status TEXT NOT NULL DEFAULT 'open',
                suggestion TEXT, created_at TEXT NOT NULL, resolved_at TEXT,
                resolution TEXT)"""
        )
        self.db.execute(
            f"""CREATE TABLE IF NOT EXISTS audit_log (
                id {ai}, ts TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, detail TEXT)"""
        )
        for code, name, classification in CHART:
            self.db.execute(
                "INSERT OR IGNORE INTO accounts (code, name, classification) VALUES (?, ?, ?)"
                if self.db.name == "sqlite"
                else "INSERT INTO accounts (code, name, classification) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (code, name, classification),
            )
        self.db.commit()

    # ---- audit ----
    def audit(self, actor: str, action: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO audit_log (ts, actor, action, detail) VALUES (?, ?, ?, ?)",
            (datetime.utcnow().isoformat(timespec="seconds"), actor, action, detail),
        )

    # ---- postings ----
    def already_ingested(self, idempotency_key: str) -> bool:
        cur = self.db.execute(
            "SELECT 1 FROM ingestion_log WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        return cur.fetchone() is not None

    def post_journal(
        self,
        lines: list[JournalLine],
        *,
        reference: str,
        batch_id: str,
        idempotency_key: str,
        txn_date: date,
        description: str = "",
        actor: str = "engine",
    ) -> int:
        """Post a balanced journal entry. Returns number of lines posted, or 0
        if the idempotency key was already seen (no double-booking, ever)."""
        if self.already_ingested(idempotency_key):
            return 0
        # Balance check per currency: sum(signed) == 0
        totals: dict[str, int] = {}
        for ln in lines:
            signed = ln.amount_minor if ln.dc == DEBIT else -ln.amount_minor
            totals[ln.currency] = totals.get(ln.currency, 0) + signed
        unbalanced = {c: t for c, t in totals.items() if t != 0}
        if unbalanced:
            raise ValueError(f"Unbalanced journal for {reference}: {unbalanced}")
        today = date.today().isoformat()
        self.db.executemany(
            """INSERT INTO journal_entries
               (account_code, currency, transaction_date, entry_date, dc,
                amount_minor, reference_number, description, batch_id, idempotency_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    ln.account_code,
                    ln.currency,
                    txn_date.isoformat(),
                    today,
                    ln.dc,
                    ln.amount_minor,
                    reference,
                    description,
                    batch_id,
                    idempotency_key,
                )
                for ln in lines
            ],
        )
        self.db.execute(
            "INSERT INTO ingestion_log (idempotency_key, source, ingested_at, journal_count)"
            " VALUES (?, ?, ?, ?)",
            (
                idempotency_key,
                reference,
                datetime.utcnow().isoformat(timespec="seconds"),
                len(lines),
            ),
        )
        self.audit(actor, "post_journal", f"{reference} ({len(lines)} lines)")
        self.db.commit()
        return len(lines)

    # ---- reads ----
    def trial_balance(self, start: str, end: str) -> dict[str, int]:
        """Net signed movement per currency in [start, end]: 0 == balanced."""
        cur = self.db.execute(
            """SELECT currency,
                      SUM(CASE WHEN dc='DEBIT' THEN amount_minor ELSE -amount_minor END)
               FROM journal_entries
               WHERE transaction_date >= ? AND transaction_date <= ?
               GROUP BY currency""",
            (start, end),
        )
        return {row[0]: row[1] for row in cur.fetchall()}

    def account_balance(self, account_code: str, currency: str | None = None) -> int:
        q = """SELECT SUM(CASE WHEN dc='DEBIT' THEN amount_minor ELSE -amount_minor END)
               FROM journal_entries WHERE account_code = ?"""
        params: list = [account_code]
        if currency:
            q += " AND currency = ?"
            params.append(currency)
        cur = self.db.execute(q, params)
        return cur.fetchone()[0] or 0

    def assert_balanced(self, start: str, end: str) -> list[str]:
        """Close gate (PRD §5.6): non-empty return BLOCKS the close and itemizes
        the imbalance per currency."""
        return [
            f"{ccy}: out of balance by {imb} minor units"
            for ccy, imb in self.trial_balance(start, end).items()
            if imb != 0
        ]

    def add_exception(
        self,
        reason_code: str,
        *,
        record_id: str = "",
        psp: str = "",
        merchant_reference: str = "",
        currency: str = "",
        amount_minor: int = 0,
        detail: str = "",
        suggestion: str = "",
        actor: str = "engine",
    ) -> int:
        if self.db.name == "postgres":
            cur = self.db.execute(
                """INSERT INTO exceptions
                   (reason_code, record_id, psp, merchant_reference, currency,
                    amount_minor, detail, suggestion, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (
                    reason_code,
                    record_id,
                    psp,
                    merchant_reference,
                    currency,
                    amount_minor,
                    detail,
                    suggestion,
                    datetime.utcnow().isoformat(timespec="seconds"),
                ),
            )
            exc_id = cur.fetchone()[0]
        else:
            cur = self.db.execute(
                """INSERT INTO exceptions
                   (reason_code, record_id, psp, merchant_reference, currency,
                    amount_minor, detail, suggestion, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    reason_code,
                    record_id,
                    psp,
                    merchant_reference,
                    currency,
                    amount_minor,
                    detail,
                    suggestion,
                    datetime.utcnow().isoformat(timespec="seconds"),
                ),
            )
            exc_id = cur.lastrowid
        self.audit(actor, "exception", f"{reason_code} {record_id} {detail}")
        self.db.commit()
        return exc_id
