"""Auto-heal (PRD §5.4): deterministic remediations. No ML here — every action
is a rule with an audit trail.

  - missed/delayed file -> re-poll the PSP, age the expectation, alert only
    after the PSP's normal window + buffer
  - FX drift within tolerance -> booked to FX gain/loss at match time
    (see matching/engine.py); the healer tracks drift stats
  - duplicates -> flagged, never double-booked (see ingest.py)
  - fee != contracted schedule -> exception with expected-vs-actual delta
    (see ingest.py)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from ..ledger.ledger import Ledger
from ..parsers import PARSERS
from .. import ingest as ingest_mod


def expected_files(
    ledger: Ledger, config: dict, start: date, end: date
) -> list[tuple[str, date]]:
    """Expected settlement files, derived from actual authorizations: for every
    order authorized in the period, its PSP owes a settlement file on
    auth_date + lag. (A PSP sends no file when there was no volume — expecting
    files for every calendar day would false-positive on quiet days.)"""
    cur = ledger.db.execute(
        "SELECT DISTINCT psp, auth_date FROM orders"
        " WHERE auth_date >= ? AND auth_date <= ?",
        (start.isoformat(), end.isoformat()),
    )
    out = []
    for psp, auth_date in cur.fetchall():
        lag = config["psps"][psp]["settlement_lag_days"]
        out.append((psp, datetime.strptime(auth_date, "%Y-%m-%d").date()
                    + timedelta(days=lag)))
    return out


def ingested_batches(ledger: Ledger) -> set[str]:
    cur = ledger.db.execute(
        "SELECT DISTINCT batch_id FROM settlement_records"
    )
    return {r[0] for r in cur.fetchall()}


def find_missing_files(
    ledger: Ledger, config: dict, start: date, end: date, as_of: date
) -> list[tuple[str, date]]:
    """Files expected but not ingested, past the PSP's lag + buffer window."""
    have = ingested_batches(ledger)
    buf = config["matching"]["date_buffer_days"]
    missing = []
    for psp, d in expected_files(ledger, config, start, end):
        if f"{psp}:{d.isoformat()}" in have:
            continue
        if d + timedelta(days=buf) <= as_of:
            missing.append((psp, d))
    return sorted(set(missing))


def repoll(
    ledger: Ledger,
    config: dict,
    missing: list[tuple[str, date]],
    files_dir: str | Path,
    withheld_dir: str | Path | None = None,
) -> dict:
    """Re-poll the PSP for missing files. In production this hits the PSP's
    report API / SFTP; in the demo it picks up files from the withheld dir."""
    files_dir = Path(files_dir)
    results: dict = {"repolled": [], "still_missing": []}
    for psp, d in missing:
        fname = f"{d.isoformat()}.csv"
        src = files_dir / psp / fname
        if not src.exists() and withheld_dir:
            w = Path(withheld_dir) / psp / fname
            if w.exists():
                src = w  # the "PSP" finally delivered it
        if src.exists():
            r = ingest_mod.ingest_file(ledger, PARSERS[psp](), src, config)
            results["repolled"].append((psp, d, r["status"]))
            ledger.audit(
                "healer", "repoll",
                f"{psp} {d.isoformat()}: {r['status']}",
            )
        else:
            results["still_missing"].append((psp, d))
            ledger.add_exception(
                "MISSING_FILE",
                psp=psp,
                detail=f"settlement file for {d.isoformat()} still missing after re-poll",
                suggestion="escalate to PSP support; age the expectation",
            )
    ledger.db.commit()
    return results


def resolve_healed_exceptions(ledger: Ledger) -> int:
    """Close MISSING_PAYMENT exceptions whose orders have since settled."""
    cur = ledger.db.execute(
        """SELECT e.id, e.merchant_reference FROM exceptions e
           JOIN orders o ON o.merchant_reference = e.merchant_reference
           WHERE e.reason_code='MISSING_PAYMENT' AND e.status='open'
             AND o.status='settled'"""
    )
    rows = cur.fetchall()
    for (exc_id, _) in rows:
        ledger.db.execute(
            "UPDATE exceptions SET status='resolved', resolved_at=?,"
            " resolution='auto-healed: settlement arrived on re-poll' WHERE id=?",
            (date.today().isoformat(), exc_id),
        )
    ledger.db.commit()
    if rows:
        ledger.audit("healer", "resolve_healed", f"{len(rows)} exceptions auto-resolved")
    return len(rows)
