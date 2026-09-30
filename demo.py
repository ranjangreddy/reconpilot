#!/usr/bin/env python3
"""ReconPilot 90-second demo (PRD §8).

Gr4vy got you to 99.9% authorization. This closes the books.

Acts:
  1. ingest orders + the adversarial settlement batch
  2. run the 3-tier matcher, report the auto-match rate
  3. a missed settlement file -> detect, re-poll, auto-heal
  4. build the close pack (gate + HTML report)
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from reconpilot.ledger.backend import SQLiteBackend
from reconpilot.ledger.ledger import Ledger
from reconpilot import ingest as ingest_mod
from reconpilot.parsers import PARSERS
from reconpilot.matching.engine import MatchEngine
from reconpilot.matching.scorer import MatchScorer
from reconpilot.autoheal import healer
from reconpilot.closepack import close as closepack


def step(msg: str) -> None:
    print(f"\n{'=' * 64}\n{msg}\n{'=' * 64}")


def match_rate(stats: dict) -> float:
    matched = stats["tier1"] + stats["tier2"] + stats["tier3"]
    return matched / stats["total"] if stats["total"] else 1.0


def main() -> int:
    t0 = time.time()
    cfg = yaml.safe_load(open(ROOT / "config.yaml"))
    data = ROOT / "data"
    db = data / "reconpilot.db"
    if db.exists():
        db.unlink()
    os.environ["RECONPILOT_DB"] = str(db)

    step("ACT 1 — ingest authorizations + PSP settlement files")
    ledger = Ledger(SQLiteBackend(str(db)))
    orders_df = pd.read_csv(data / "orders.csv", dtype={"merchant_reference": str})
    orders_df["auth_date"] = orders_df["auth_date"].astype(str)
    n_orders = ingest_mod.ingest_orders(ledger, orders_df.to_dict("records"))
    print(f"  {n_orders} orders posted to in-transit (1100)")
    n_ok = n_quar = 0
    for psp in ("adyen", "stripe", "shift4"):
        for f in sorted((data / "psp_files" / psp).glob("*.csv")):
            r = ingest_mod.ingest_file(ledger, PARSERS[psp](), f, cfg)
            n_ok += r["status"] == "ingested"
            n_quar += r["status"] == "quarantined"
    print(f"  {n_ok} files ingested, {n_quar} quarantined")

    step("ACT 2 — three-tier matching (exact -> tolerant -> ML)")
    scorer = MatchScorer(ROOT / "models" / "match_scorer.pkl")
    scorer.bootstrap()
    engine = MatchEngine(ledger, cfg, scorer)
    stats = engine.run()
    matched = stats["tier1"] + stats["tier2"] + stats["tier3"]
    print(f"  auto-match rate: {match_rate(stats):.1%} "
          f"({matched}/{stats['total']})")
    print(f"  tiers: {stats['tier1']} exact · {stats['tier2']} tolerant · "
          f"{stats['tier3']} ML-assisted · {stats['exceptions']} exceptions "
          f"({stats['missing_legs']} missing legs)")

    step("ACT 3 — missed file: detect -> re-poll -> heal")
    start = date.fromisoformat(cfg["demo"]["period_start"])
    end = date.fromisoformat(cfg["demo"]["period_end"])
    as_of = end + timedelta(days=10)
    missing = healer.find_missing_files(ledger, cfg, start, end, as_of)
    print(f"  missing files past the lag+buffer window: {len(missing)}")
    res = healer.repoll(ledger, cfg, missing, data / "psp_files",
                        data / "withheld")
    for psp, d, status in res["repolled"]:
        print(f"  re-polled {psp} {d}: {status}")
    for psp, d in res["still_missing"]:
        print(f"  STILL MISSING: {psp} {d} -> exception raised")
    stats = engine.run()  # match the newly arrived records
    healed = healer.resolve_healed_exceptions(ledger)
    print(f"  match rate after heal: {match_rate(stats):.1%} "
          f"({stats['tier1'] + stats['tier2'] + stats['tier3']}/{stats['total']} new), "
          f"{healed} exceptions auto-resolved")
    blockers = ledger.assert_balanced(start.isoformat(), end.isoformat())
    print(f"  ledger balanced: {'yes ✓' if not blockers else blockers}")

    step("ACT 4 — close pack")
    reports = ROOT / "reports"
    if reports.exists():
        shutil.rmtree(reports)
    rep = closepack.build(ledger, cfg, cfg["demo"]["period_start"][:7],
                          reports, start.isoformat(), end.isoformat(), as_of=as_of)
    gate = rep["gate"]
    print(f"  close gate: {'PASSED' if gate['ok'] else 'BLOCKED — the books do not close on vibes'}")
    for b in gate["blockers"]:
        print(f"    blocker: {b}")
    if not gate["ok"]:
        cur = ledger.db.execute(
            "SELECT reason_code, COUNT(*) FROM exceptions WHERE status='open'"
            " GROUP BY reason_code ORDER BY COUNT(*) DESC"
        )
        print("  open exceptions by type (the analyst's queue):")
        for code, n in cur.fetchall():
            print(f"    {code:<28} {n:>4}")
        print("  resolve them in the workbench: "
              "python -m reconpilot.workbench.cli exceptions")
    print("  fee variance vs rate card:")
    for f in rep["fee_variance"]:
        print(f"    {f['psp']:<8} effective {f['effective_rate']:.2%} "
              f"vs contracted {f['contracted_rate']:.2%} "
              f"({f['variance_bps']:+.0f} bps)")
    print(f"  HTML close pack: {rep['html']}")
    print(f"\nDone in {time.time() - t0:.1f}s. Ledger: {db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
