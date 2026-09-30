"""Close pack (PRD §5.8): the month-end gate. Books don't close on vibes.

  1. assert_balanced — the ledger itself must balance, or the close is blocked
  2. all exceptions resolved — nothing open leaks into the new period
  3. fee variance per PSP vs the contracted rate card
  4. cash forecast from per-PSP average settlement lag
  5. CSV exports + a static HTML close-pack report for finance
"""
from __future__ import annotations

import csv
import statistics
from datetime import date, timedelta
from pathlib import Path

from ..ledger.ledger import Ledger


def gate_close(ledger: Ledger, start: str, end: str) -> dict:
    """Returns {'ok': bool, 'blockers': [...]}. ok=False blocks the close."""
    blockers = list(ledger.assert_balanced(start, end))
    cur = ledger.db.execute(
        "SELECT COUNT(*) FROM exceptions WHERE status='open'"
    )
    n_open = cur.fetchone()[0]
    if n_open:
        blockers.append(f"{n_open} open exceptions")
    cur = ledger.db.execute(
        "SELECT COUNT(*) FROM settlement_records WHERE match_status='unmatched'"
    )
    n_un = cur.fetchone()[0]
    if n_un:
        blockers.append(f"{n_un} unmatched settlement records")
    return {"ok": not blockers, "blockers": blockers}


def fee_variance(ledger: Ledger, config: dict) -> list[dict]:
    """Effective fee rate per PSP vs contracted rate card."""
    out = []
    for psp, pcfg in config["psps"].items():
        cur = ledger.db.execute(
            """SELECT SUM(ABS(gross_amount)), SUM(ABS(fee_amount))
               FROM settlement_records
               WHERE psp=? AND record_type='payment' AND match_status != 'unmatched'""",
            (psp,),
        )
        gross, fee = cur.fetchone()
        if not gross:
            continue
        eff = fee / gross
        contracted = pcfg["contracted_fee_rate"]
        out.append({
            "psp": psp, "volume": gross / 100, "fees": fee / 100,
            "effective_rate": eff, "contracted_rate": contracted,
            "variance_bps": (eff - contracted) * 10000,
        })
    return out


def cash_forecast(ledger: Ledger, config: dict, as_of: date) -> list[dict]:
    """Project cash-in from open (authorized, not yet settled) orders using
    each PSP's average observed settlement lag."""
    out = []
    for psp, pcfg in config["psps"].items():
        cur = ledger.db.execute(
            """SELECT o.auth_date, s.settlement_date FROM settlement_records s
               JOIN orders o ON o.merchant_reference = s.matched_order_ref
               WHERE s.psp=? AND s.match_status='matched'""",
            (psp,),
        )
        lags = [
            (date.fromisoformat(r[1]) - date.fromisoformat(r[0])).days
            for r in cur.fetchall()
        ]
        avg_lag = statistics.mean(lags) if lags else pcfg["settlement_lag_days"]
        cur = ledger.db.execute(
            """SELECT auth_date, SUM(amount_minor) FROM orders
               WHERE psp=? AND status='open' GROUP BY auth_date""",
            (psp,),
        )
        for auth_date, total in cur.fetchall():
            expected = (date.fromisoformat(auth_date)
                        + timedelta(days=round(avg_lag)))
            if expected >= as_of:
                out.append({
                    "psp": psp, "expected_date": expected.isoformat(),
                    "amount": total / 100, "currency": "mixed",
                })
    return sorted(out, key=lambda r: r["expected_date"])


def export_csvs(ledger: Ledger, outdir: Path) -> list[str]:
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for table in ("settlement_records", "exceptions", "journal_entries",
                  "ingestion_log"):
        cur = ledger.db.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description]
        p = outdir / f"{table}.csv"
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(cur.fetchall())
        paths.append(str(p))
    return paths


def render_html(report: dict, outpath: Path) -> str:
    fees = "\n".join(
        f"<tr><td>{f['psp']}</td><td>{f['volume']:,.2f}</td>"
        f"<td>{f['fees']:,.2f}</td><td>{f['effective_rate']:.2%}</td>"
        f"<td>{f['contracted_rate']:.2%}</td>"
        f"<td>{f['variance_bps']:+.0f} bps</td></tr>"
        for f in report["fee_variance"]
    )
    fc = "\n".join(
        f"<tr><td>{f['expected_date']}</td><td>{f['psp']}</td>"
        f"<td>{f['amount']:,.2f}</td></tr>"
        for f in report["cash_forecast"][:15]
    )
    exc = "\n".join(
        f"<tr><td>{e['reason_code']}</td><td>{e['psp'] or '-'}</td>"
        f"<td>{e['detail'][:80]}</td><td>{e['resolution'] or '-'}</td></tr>"
        for e in report["exceptions"]
    )
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>ReconPilot close pack — {report['period']}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;
color:#1a1a1a}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}
th,td{{border:1px solid #ccc;padding:.4rem .6rem;text-align:left;font-size:.85rem}}
th{{background:#f4f4f4}}.ok{{color:#0a7d2c;font-weight:700}}
.bad{{color:#b3261e;font-weight:700}}</style></head><body>
<h1>ReconPilot close pack — {report['period']}</h1>
<p>Close gate: <span class="{'ok' if report['gate']['ok'] else 'bad'}">
{'PASSED' if report['gate']['ok'] else 'BLOCKED: ' + '; '.join(report['gate']['blockers'])}</span></p>
<p>Auto-match rate: <b>{report['match_rate']:.1%}</b>
({report['matched']} of {report['total']} settlement records)</p>
<h2>Fee variance vs rate card</h2>
<table><tr><th>PSP</th><th>Volume</th><th>Fees</th><th>Effective</th>
<th>Contracted</th><th>Variance</th></tr>{fees}</table>
<h2>Cash forecast (open authorizations)</h2>
<table><tr><th>Expected</th><th>PSP</th><th>Amount</th></tr>{fc}</table>
<h2>Exceptions log</h2>
<table><tr><th>Code</th><th>PSP</th><th>Detail</th><th>Resolution</th></tr>{exc}</table>
<p><small>Generated {report['generated_at']} · ReconPilot v0</small></p>
</body></html>"""
    outpath.write_text(html)
    return str(outpath)


def build(ledger: Ledger, config: dict, period: str, outdir: Path,
          start: str, end: str, as_of: date | None = None) -> dict:
    as_of = as_of or date.today()
    gate = gate_close(ledger, start, end)
    cur = ledger.db.execute(
        "SELECT COUNT(*), SUM(CASE WHEN match_status='matched' THEN 1 ELSE 0 END)"
        " FROM settlement_records"
    )
    total, matched = cur.fetchone()
    report = {
        "period": period,
        "generated_at": as_of.isoformat(),
        "gate": gate,
        "total": total or 0,
        "matched": matched or 0,
        "match_rate": (matched / total) if total else 0.0,
        "fee_variance": fee_variance(ledger, config),
        "cash_forecast": cash_forecast(ledger, config, as_of),
        "exceptions": [
            dict(r) for r in ledger.db.execute(
                "SELECT reason_code, psp, detail, resolution, status FROM exceptions"
            ).fetchall()
        ],
        "exports": export_csvs(ledger, outdir),
        "html": render_html(
            {"period": period, "gate": gate,
             "match_rate": (matched / total) if total else 0.0,
             "matched": matched or 0, "total": total or 0,
             "fee_variance": fee_variance(ledger, config),
             "cash_forecast": cash_forecast(ledger, config, as_of),
             "exceptions": [
                 dict(r) for r in ledger.db.execute(
                     "SELECT reason_code, psp, detail, resolution FROM exceptions"
                 ).fetchall()
             ],
             "generated_at": as_of.isoformat()},
            outdir / f"close-pack-{period}.html",
        ),
    }
    return report
