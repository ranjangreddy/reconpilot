"""Match rate: a small seeded batch with faults must auto-match >= 95%.

Reuses the real seed builders (reconpilot.seed.generate) so the test exercises
the same file formats as the demo, with one orphan + one duplicate injected.
"""
from __future__ import annotations

import random
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from reconpilot import ingest as ingest_mod
from reconpilot.matching.engine import MatchEngine
from reconpilot.parsers import PARSERS
from reconpilot.seed import generate as gen
from tests.helpers import CONFIG, make_ledger, make_scorer

PSPS = ["adyen", "stripe", "shift4"]


def build_batch(tmp: str, seed: int = 1234):
    rng = random.Random(seed)
    cfg = CONFIG
    fee_rate = {p: cfg["psps"][p]["contracted_fee_rate"] for p in PSPS}
    lag = {p: cfg["psps"][p]["settlement_lag_days"] for p in PSPS}

    orders, recs = [], {p: [] for p in PSPS}
    for i in range(60):
        psp = PSPS[i % 3]
        ref = f"T-{i:04d}"
        amount = rng.randint(1000, 50000)
        ccy = rng.choice(["GBP", "EUR", "USD"])
        auth = date(2011, 1, 3) + timedelta(days=rng.randint(0, 17))
        settle = (auth + timedelta(days=lag[psp])).isoformat()
        orders.append({"merchant_reference": ref, "amount_minor": amount,
                       "currency": ccy, "auth_date": auth.isoformat(), "psp": psp})
        recs[psp].append({
            "psp_ref": f"{psp[:3].upper()}{i:013d}"[:16],
            "merchant_ref": ref, "gross": amount, "currency": ccy,
            "auth_date": auth.isoformat(), "settle_date": settle,
            "record_type": "payment", "batch": f"{psp}:{settle}", "fx_rate": "1.0",
        })
    # fault 1: orphan settlement (no such order)
    recs["adyen"].append({
        "psp_ref": "ADY9999999999999", "merchant_ref": "GHOST-1", "gross": 12345,
        "currency": "GBP", "auth_date": "2011-01-05", "settle_date": "2011-01-07",
        "record_type": "payment", "batch": "adyen:2011-01-07", "fx_rate": "1.0",
    })
    # fault 2: within-file duplicate
    recs["stripe"].append(dict(recs["stripe"][0]))

    files = []
    for psp, rs in recs.items():
        build, cols = gen.BUILDERS[psp]
        by_day: dict[str, list] = {}
        for r in rs:
            by_day.setdefault(r["settle_date"], []).append(r)
        for day, day_recs in sorted(by_day.items()):
            rows = build(day_recs, fee_rate[psp])
            nets: dict[str, int] = {}
            for r, row in zip(day_recs, rows):
                nets[r["currency"]] = nets.get(r["currency"], 0) + gen._row_net(psp, row)
            rows += gen._payout_rows(psp, day, nets, cols)
            p = Path(tmp) / psp / f"{day}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows, columns=cols).to_csv(p, index=False)
            files.append((psp, p))
    return orders, files


class TestMatchRate(unittest.TestCase):
    def test_auto_match_rate_at_least_95(self):
        ledger = make_ledger()
        with TemporaryDirectory() as tmp:
            orders, files = build_batch(tmp)
            ingest_mod.ingest_orders(ledger, orders)
            for psp, p in files:
                res = ingest_mod.ingest_file(ledger, PARSERS[psp](), p, CONFIG)
                self.assertEqual(res["status"], "ingested", f"{psp} {p.name}")
            scorer = make_scorer(tmp)
            stats = MatchEngine(ledger, CONFIG, scorer).run()
        matched = stats["tier1"] + stats["tier2"] + stats["tier3"]
        rate = matched / stats["total"]
        print(f"\n  match rate: {rate:.1%} ({matched}/{stats['total']}) "
              f"t1={stats['tier1']} t2={stats['tier2']} t3={stats['tier3']} "
              f"exc={stats['exceptions']}")
        self.assertGreaterEqual(rate, 0.95)


if __name__ == "__main__":
    unittest.main()
