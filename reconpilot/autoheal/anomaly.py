"""Statistical anomaly detection (PRD §5.7): settlement delays and fee creep.

Unsupervised and explainable — every alert cites the baseline it deviated
from. This is the "slow bleed" detector: the things humans miss because no
single transaction looks wrong.
"""
from __future__ import annotations

import statistics
from datetime import datetime

from ..ledger.ledger import Ledger


def _d(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


def detect_settlement_delays(ledger: Ledger, config: dict) -> list[dict]:
    """Per-PSP mean settlement lag vs the contracted lag: flag drift."""
    alerts = []
    for psp, pcfg in config["psps"].items():
        cur = ledger.db.execute(
            """SELECT o.auth_date, s.settlement_date FROM settlement_records s
               JOIN orders o ON o.merchant_reference = s.matched_order_ref
               WHERE s.psp=? AND s.match_status='matched'""",
            (psp,),
        )
        lags = [(_d(r[1]) - _d(r[0])).days for r in cur.fetchall()]
        if len(lags) < 10:
            continue
        mean_lag = statistics.mean(lags)
        expected = pcfg["settlement_lag_days"]
        if mean_lag > expected + 1.5:
            alerts.append({
                "type": "SETTLEMENT_DELAY",
                "psp": psp,
                "detail": f"mean lag {mean_lag:.1f}d vs contracted {expected}d "
                          f"over {len(lags)} settlements",
            })
    return alerts


def detect_fee_creep(ledger: Ledger, config: dict) -> list[dict]:
    """Effective fee rate per PSP, recent week vs trailing baseline."""
    alerts = []
    for psp, pcfg in config["psps"].items():
        cur = ledger.db.execute(
            """SELECT substr(settlement_date, 1, 7) AS ym,
                      SUM(ABS(gross_amount)), SUM(ABS(fee_amount))
               FROM settlement_records
               WHERE psp=? AND record_type='payment' AND match_status != 'unmatched'
               GROUP BY ym ORDER BY ym""",
            (psp,),
        )
        months = [(r[0], r[1] / r[2] if r[2] else 0) for r in cur.fetchall()]
        if len(months) < 2:
            continue
        baseline = sum(r for _, r in months[:-1]) / (len(months) - 1)
        latest = months[-1][1]
        contracted = pcfg["contracted_fee_rate"]
        if latest > baseline + 0.005:  # 50 bps creep
            alerts.append({
                "type": "FEE_CREEP",
                "psp": psp,
                "detail": f"effective rate {latest:.2%} in {months[-1][0]} vs "
                          f"{baseline:.2%} baseline (contracted {contracted:.2%})",
            })
    return alerts


def run_all(ledger: Ledger, config: dict) -> list[dict]:
    alerts = detect_settlement_delays(ledger, config) + detect_fee_creep(ledger, config)
    for a in alerts:
        ledger.add_exception(
            "ANOMALY_" + a["type"],
            psp=a["psp"],
            detail=a["detail"],
            suggestion="review PSP performance / rate card",
            actor="anomaly",
        )
    return alerts
