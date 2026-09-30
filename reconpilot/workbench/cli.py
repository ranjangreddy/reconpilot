"""Workbench CLI (PRD §5.6): review the exceptions queue, resolve by hand,
feedback becomes training data.

The analyst's desk. Every exception shows the evidence, the analyst picks the
resolution, and match resolutions flow back into the scorer's training set —
the loop that makes the ML component improve without ever touching the ledger.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, date
from pathlib import Path

import yaml

from ..ledger.backend import SQLiteBackend
from ..ledger.ledger import CREDIT, DEBIT, JournalLine, Ledger
from ..matching.engine import MatchEngine
from ..matching.scorer import MatchScorer, featurize


def _ledger(db_path: str) -> Ledger:
    return Ledger(SQLiteBackend(db_path))


def cmd_list(args) -> int:
    ledger = _ledger(args.db)
    cur = ledger.db.execute(
        "SELECT id, reason_code, psp, merchant_reference, detail, suggestion"
        " FROM exceptions WHERE status='open' ORDER BY created_at, id"
    )
    rows = cur.fetchall()
    if not rows:
        print("No open exceptions. The books are clean.")
        return 0
    print(f"{'ID':>5}  {'CODE':<24} {'PSP':<8} {'REF':<14} DETAIL")
    print("-" * 100)
    for r in rows:
        print(f"{r[0]:>5}  {r[1]:<24} {r[2] or '-':<8} {r[3] or '-':<14} "
              f"{(r[4] or '')[:52]}")
    return 0


def cmd_show(args) -> int:
    ledger = _ledger(args.db)
    cur = ledger.db.execute("SELECT * FROM exceptions WHERE id=?", (args.id,))
    row = cur.fetchone()
    if not row:
        print(f"no exception #{args.id}")
        return 1
    d = dict(row)
    for k, v in d.items():
        print(f"{k:>20}: {v}")
    return 0


def cmd_resolve(args) -> int:
    """resolve <id> --to <order_ref> | --refund | --writeoff | --ignore

    A match resolution teaches the scorer: the matched pair is appended to the
    training set and the model is retrained (PRD §5.6, AI-proposes-rules-dispose).
    """
    ledger = _ledger(args.db)
    cfg = yaml.safe_load(open(args.config))
    cur = ledger.db.execute("SELECT * FROM exceptions WHERE id=?", (args.id,))
    row = cur.fetchone()
    if not row:
        print(f"no exception #{args.id}")
        return 1
    exc = dict(row)

    if args.to:  # manual match
        cfg = yaml.safe_load(open(args.config))
        scorer = MatchScorer("models/match_scorer.pkl")
        engine = MatchEngine(ledger, cfg, scorer)
        rec = _record_for_exception(ledger, exc)
        order = _order(ledger, args.to)
        if rec is None or order is None:
            print("exception or order not found")
            return 1
        engine._post_match(rec, order, 4, 1.0, "manual: analyst resolution", 0)
        # feedback -> training data: this pair is a confirmed match
        lag = cfg["psps"][order["psp"]]["settlement_lag_days"]
        feat = featurize(
            abs(order["gross"] - rec["gross_amount"]) / max(order["gross"], 1),
            abs((date.fromisoformat(rec["settlement_date"])
                 - date.fromisoformat(order["auth_date"])).days - lag),
            order["merchant_reference"], rec["merchant_reference"],
            True, order["psp"] == rec["psp"], 0.0,
        )
        scorer.append_resolution(feat, 1)
        resolution = f"manual match -> {args.to}"
    elif args.refund:
        ledger.post_journal(
            [
                JournalLine("4000", DEBIT, exc["amount_minor"] or 0,
                            exc["currency"] or "GBP"),
                JournalLine("1000", CREDIT, exc["amount_minor"] or 0,
                            exc["currency"] or "GBP"),
            ],
            reference=f"manual-refund-{args.id}",
            batch_id="workbench",
            idempotency_key=f"manual-refund-{args.id}",
            txn_date=date.today(),
            description=f"manual refund for exception #{args.id}",
            actor="analyst",
        )
        resolution = "refunded manually"
    elif args.writeoff:
        ledger.post_journal(
            [
                JournalLine("5010", DEBIT, exc["amount_minor"] or 0,
                            exc["currency"] or "GBP"),
                JournalLine("1100", CREDIT, exc["amount_minor"] or 0,
                            exc["currency"] or "GBP"),
            ],
            reference=f"manual-wo-{args.id}",
            batch_id="workbench",
            idempotency_key=f"manual-wo-{args.id}",
            txn_date=date.today(),
            description=f"write-off for exception #{args.id}",
            actor="analyst",
        )
        resolution = "written off to FX variance"
    else:
        resolution = "dismissed by analyst (no action)"

    ledger.db.execute(
        "UPDATE exceptions SET status='resolved', resolved_at=?, resolution=?"
        " WHERE id=?",
        (date.today().isoformat(), resolution, args.id),
    )
    ledger.db.commit()
    ledger.audit("analyst", "resolve_exception",
                 f"#{args.id}: {resolution}")
    print(f"exception #{args.id} resolved: {resolution}")
    return 0


def _record_for_exception(ledger: Ledger, exc: dict):
    cur = ledger.db.execute(
        "SELECT * FROM settlement_records WHERE record_id=? AND psp=?",
        (exc.get("record_id"), exc.get("psp")),
    )
    r = cur.fetchone()
    return dict(r) if r else None


def _order(ledger: Ledger, ref: str):
    cur = ledger.db.execute(
        "SELECT merchant_reference, amount_minor AS gross, currency, auth_date, psp"
        " FROM orders WHERE merchant_reference=?",
        (ref,),
    )
    r = cur.fetchone()
    return dict(r) if r else None


def cmd_stats(args) -> int:
    ledger = _ledger(args.db)
    cur = ledger.db.execute(
        "SELECT match_status, COUNT(*), SUM(net_amount) FROM settlement_records"
        " GROUP BY match_status"
    )
    print(f"{'STATUS':<22} {'COUNT':>8} {'NET_TOTAL':>12}")
    for status, cnt, net in cur.fetchall():
        print(f"{status:<22} {cnt:>8} {(net or 0) / 100:>12.2f}")
    cur = ledger.db.execute(
        "SELECT reason_code, COUNT(*) FROM exceptions GROUP BY reason_code"
    )
    print("\nexceptions:")
    for code, cnt in cur.fetchall():
        print(f"  {code:<24} {cnt:>6}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reconpilot",
                                 description="ReconPilot analyst workbench")
    ap.add_argument("--db", default="data/reconpilot.db")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("exceptions", help="list open exceptions").set_defaults(fn=cmd_list)
    p = sub.add_parser("show", help="show one exception")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("resolve", help="resolve one exception")
    p.add_argument("id", type=int)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--to", metavar="ORDER_REF", help="match to this order")
    g.add_argument("--refund", action="store_true")
    g.add_argument("--writeoff", action="store_true")
    g.add_argument("--ignore", action="store_true")
    p.set_defaults(fn=cmd_resolve)
    sub.add_parser("stats", help="match + exception stats").set_defaults(fn=cmd_stats)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
