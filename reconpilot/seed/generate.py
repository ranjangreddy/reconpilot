"""Seed data generator: UCI Online Retail II -> three PSP settlement formats.

Real transaction shapes (invoice IDs, amounts, timestamps, countries from a
real UK retailer), mapped into Adyen / Stripe / Shift4 file formats built to
their public column specs. Deterministic (seed 42).

Fault injection (the demo's adversarial batch):
  - one withheld Adyen file        -> missed-file auto-heal demo
  - FX drift on ~2% of Stripe rows -> tier-2 tolerant match + FX variance booking
  - ~0.4% within-file duplicates  -> POSSIBLE_DUPLICATE_CAPTURE
  - one Stripe day at 2x fee rate  -> FEE_VARIANCE exceptions
  - ~12 orphan settlements         -> ORPHAN_PAYMENT
  - ~12 orders with no settlement  -> MISSING_PAYMENT
"""
from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import yaml

COUNTRY_CCY = {
    "United Kingdom": "GBP",
    "EIRE": "EUR",
    "Germany": "EUR", "France": "EUR", "Netherlands": "EUR", "Belgium": "EUR",
    "Spain": "EUR", "Italy": "EUR", "Portugal": "EUR", "Austria": "EUR",
    "Switzerland": "EUR", "Ireland": "EUR", "Norway": "EUR", "Denmark": "EUR",
    "Sweden": "EUR", "Finland": "EUR", "Poland": "EUR", "Greece": "EUR",
    "USA": "USD", "Australia": "USD", "Canada": "USD",
}

PSP_WEIGHTS = [("adyen", 0.40), ("stripe", 0.35), ("shift4", 0.25)]


def load_orders(xlsx: Path, start: date, end: date, seed: int = 42):
    """One row per invoice: gross = positive-quantity lines only.

    (This copy of the UCI file encodes cancellations as C-prefix invoices
    with negative quantities; their originals predate the demo window, so
    refunds are modeled instead — see generate().)
    """
    df = pd.read_excel(xlsx, usecols=["Invoice", "Quantity", "InvoiceDate", "Price",
                                      "Customer ID", "Country"])
    df = df.dropna(subset=["Invoice", "Customer ID"])
    df["Invoice"] = df["Invoice"].astype(str).str.replace(r"\.0$", "", regex=True)
    df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"]).dt.date
    df = df[(df["InvoiceDate"] >= start) & (df["InvoiceDate"] <= end)]
    df = df[df["Price"] > 0]
    df["line_total"] = (df["Quantity"] * df["Price"]).round(2)
    pos = df[df["Quantity"] > 0]
    orders = (
        pos.groupby(["Invoice", "InvoiceDate", "Country"], as_index=False)["line_total"]
        .sum()
    )
    orders = orders[orders["line_total"] > 0].copy()

    rng = random.Random(seed)
    psp_names, weights = zip(*PSP_WEIGHTS)
    orders["psp"] = [rng.choices(psp_names, weights)[0] for _ in range(len(orders))]
    orders["currency"] = orders["Country"].map(COUNTRY_CCY).fillna("GBP")
    orders["amount_minor"] = (orders["line_total"] * 100).round().astype(int)
    orders = orders.rename(columns={"Invoice": "merchant_reference", "InvoiceDate": "auth_date"})
    return orders


def _fee(gross: int, fee_rate: float) -> int:
    # PSPs keep the original fee on refunds — the refund itself carries no fee.
    if gross < 0:
        return 0
    return int(round(gross * fee_rate))


def _adyen_rows(recs: list[dict], fee_rate: float) -> list[dict]:
    rows = []
    for r in recs:
        fee = _fee(r["gross"], fee_rate)
        comm, mark, inter = int(fee * 0.4), int(fee * 0.2), int(fee * 0.3)
        parts = {
            "Commission (NC)": comm, "Markup (NC)": mark,
            "Interchange (NC)": inter, "Scheme Fees (NC)": fee - comm - mark - inter,
        }
        rows.append({
            "Company Account": "DemoCompany", "Merchant Account": "DemoMerchant",
            "Psp Reference": r["psp_ref"], "Merchant Reference": r["merchant_ref"],
            "Payment Method": "visa", "Creation Date": r["auth_date"],
            "TimeZone": "Europe/London", "Type": "Payment",
            "Modification Reference": "", "Record Type": {"payment": "Settled",
                                                          "refund": "Refunded"}[r["record_type"]],
            "Gross Currency": r["currency"], "Gross (NC)": r["gross"],
            "Net Currency": r["currency"], "Net (NC)": r["gross"] - fee,
            **parts, "Exchange Rate": r.get("fx_rate", "1.0"),
            "Batch Number": r["batch"], "Batch Closed Date": r["settle_date"],
        })
    return rows


def _stripe_rows(recs: list[dict], fee_rate: float) -> list[dict]:
    rows = []
    for r in recs:
        fee = _fee(r["gross"], fee_rate)
        cat = {"payment": "charge", "refund": "refund", "chargeback": "dispute"}[r["record_type"]]
        rows.append({
            "balance_transaction_id": r["psp_ref"],
            "gross": f"{r['gross'] / 100:.2f}", "fee": f"{fee / 100:.2f}",
            "net": f"{(r['gross'] - fee) / 100:.2f}", "currency": r["currency"].lower(),
            "customer_facing_amount": f"{r['gross'] / 100:.2f}",
            "customer_facing_currency": r["currency"].lower(),
            "exchange_rate": r.get("fx_rate", "1.0"),
            "reporting_category": cat,
            "payment_intent_id": r["psp_ref"], "charge_id": "ch_" + r["psp_ref"][:14],
            "automatic_payout_id": f"po_{r['settle_date']}",
            "description": f"Order {r['merchant_ref']}",
        })
    return rows


def _shift4_rows(recs: list[dict], fee_rate: float) -> list[dict]:
    rows = []
    for r in recs:
        fee = _fee(r["gross"], fee_rate)
        rows.append({
            "statement_date": r["settle_date"], "statement_id": f"STMT-{r['settle_date']}",
            "payment_date": r["settle_date"], "payment_id": r["psp_ref"],
            "mid": "DEMO-MID-001", "transaction_date": r["auth_date"],
            "transaction_time": "12:00:00",
            "transaction_amount": f"{r['gross'] / 100:.2f}",
            "funds_status": {"payment": "settled", "refund": "refunded"}[r["record_type"]],
            "payment_currency": r["currency"],
            "merchant_discount_fee": f"{int(fee * 0.5) / 100:.2f}",
            "buy_rate_fee": f"{int(fee * 0.3) / 100:.2f}",
            "rev_share_fee": f"{(fee - int(fee * 0.8)) / 100:.2f}",
            "decline_fee": "0.00",
        })
    return rows


ADYEN_COLS = ["Company Account", "Merchant Account", "Psp Reference",
              "Merchant Reference", "Payment Method", "Creation Date", "TimeZone",
              "Type", "Modification Reference", "Record Type", "Gross Currency",
              "Gross (NC)", "Net Currency", "Net (NC)", "Commission (NC)",
              "Markup (NC)", "Interchange (NC)", "Scheme Fees (NC)",
              "Exchange Rate", "Batch Number", "Batch Closed Date"]
STRIPE_COLS = ["balance_transaction_id", "gross", "fee", "net", "currency",
               "customer_facing_amount", "customer_facing_currency",
               "exchange_rate", "reporting_category", "payment_intent_id",
               "charge_id", "automatic_payout_id", "description"]
SHIFT4_COLS = ["statement_date", "statement_id", "payment_date", "payment_id",
               "mid", "transaction_date", "transaction_time", "transaction_amount",
               "funds_status", "payment_currency", "merchant_discount_fee",
               "buy_rate_fee", "rev_share_fee", "decline_fee"]

BUILDERS = {"adyen": (_adyen_rows, ADYEN_COLS),
            "stripe": (_stripe_rows, STRIPE_COLS),
            "shift4": (_shift4_rows, SHIFT4_COLS)}


def generate(outdir: Path = Path("data"), cfg_path: Path = Path("config.yaml")) -> dict:
    cfg = yaml.safe_load(open(cfg_path))
    rng = random.Random(cfg["demo"]["random_seed"])
    start = date.fromisoformat(cfg["demo"]["period_start"])
    end = date.fromisoformat(cfg["demo"]["period_end"])

    orders = load_orders(outdir / "online_retail_II.xlsx", start, end)
    print(f"orders loaded: {len(orders)}")

    fee_rate = {p: cfg["psps"][p]["contracted_fee_rate"] for p in cfg["psps"]}
    lag = {p: cfg["psps"][p]["settlement_lag_days"] for p in cfg["psps"]}

    # ---- build settlement records per PSP ----
    psp_recs: dict[str, list[dict]] = {p: [] for p in cfg["psps"]}
    missing_leg_refs: set[str] = set()
    settled_refs: list[tuple[str, str, int, str]] = []  # (ref, psp, gross, auth)
    n = 0
    for _, o in orders.iterrows():
        n += 1
        psp = o["psp"]
        if n % 293 == 0:  # ~8 missing legs: authorized, never settled
            missing_leg_refs.add(o["merchant_reference"])
            continue
        auth = o["auth_date"]
        settle = (auth + timedelta(days=lag[psp])).isoformat()
        psp_recs[psp].append({
            "psp_ref": f"{psp[:3].upper()}{rng.randint(10**12, 10**13 - 1):013d}"[:16],
            "merchant_ref": o["merchant_reference"], "gross": int(o["amount_minor"]),
            "currency": o["currency"], "auth_date": auth.isoformat(),
            "settle_date": settle, "record_type": "payment",
            "batch": f"{psp}:{settle}", "fx_rate": "1.0",
        })
        settled_refs.append((o["merchant_reference"], psp, int(o["amount_minor"]),
                             o["currency"], auth.isoformat()))
    # modeled refunds: ~1.5% of settled Adyen/Stripe orders are fully refunded
    # a few days after settlement (real refs/amounts/dates/PSPs; the return
    # event itself is modeled — the C-invoices' originals predate the demo
    # window). Shift4 is excluded: its spec carries no order reference, so a
    # modeled Shift4 refund could never join back to its order.
    refundable = [t for t in settled_refs if t[1] != "shift4"]
    for ref, psp, gross, ccy, auth in rng.sample(refundable,
                                                 max(1, len(refundable) * 15 // 1000)):
        settle = (date.fromisoformat(auth) + timedelta(days=lag[psp] + 3)).isoformat()
        psp_recs[psp].append({
            "psp_ref": f"{psp[:3].upper()}{rng.randint(10**12, 10**13 - 1):013d}"[:16],
            "merchant_ref": ref, "gross": -gross,
            "currency": ccy, "auth_date": auth,
            "settle_date": settle, "record_type": "refund",
            "batch": f"{psp}:{settle}", "fx_rate": "1.0",
        })
    print(f"modeled refunds: {max(1, len(refundable) * 15 // 1000)}")

    # ---- fault injection ----
    # 1) FX drift on ~2% of Stripe payments: settle a hair less than authorized
    stripe_pay = [r for r in psp_recs["stripe"] if r["record_type"] == "payment"]
    for r in rng.sample(stripe_pay, max(1, len(stripe_pay) * 2 // 100)):
        drift = max(1, int(r["gross"] * rng.uniform(0.002, 0.004)))
        r["gross"] -= drift
        r["fx_rate"] = f"{1 - drift / (r['gross'] + drift):.6f}"
    # 2) within-file duplicates (~0.4%)
    for psp, recs in psp_recs.items():
        pay = [r for r in recs if r["record_type"] == "payment"]
        for r in rng.sample(pay, max(1, len(pay) * 4 // 1000)):
            recs.append(dict(r))  # same psp_ref -> duplicate
    # 3) orphan settlements (no matching order)
    for i in range(12):
        psp = rng.choice(["adyen", "stripe"])
        d = (start + timedelta(days=rng.randint(2, 27))).isoformat()
        settle = (date.fromisoformat(d) + timedelta(days=lag[psp])).isoformat()
        gross = rng.randint(1000, 20000)
        psp_recs[psp].append({
            "psp_ref": f"{psp[:3].upper()}{rng.randint(10**12, 10**13 - 1):013d}"[:16],
            "merchant_ref": f"GHOST-{i:04d}", "gross": gross,
            "currency": "GBP", "auth_date": d, "settle_date": settle,
            "record_type": "payment", "batch": f"{psp}:{settle}", "fx_rate": "1.0",
        })

    # ---- write daily files ----
    psp_dir = outdir / "psp_files"
    withheld = outdir / "withheld"
    fee_violation_day = "2010-11-12"
    withheld_file = ("adyen", "2010-11-16")
    written, withheld_count = 0, 0
    for psp, recs in psp_recs.items():
        build, cols = BUILDERS[psp]
        by_day: dict[str, list[dict]] = {}
        for r in recs:
            by_day.setdefault(r["settle_date"], []).append(r)
        for day in sorted(by_day):
            day_recs = by_day[day]
            rate = fee_rate[psp] * (2.0 if (psp == "stripe" and day == fee_violation_day) else 1.0)
            rows = build(day_recs, rate)
            # PaidOut summary rows per currency (the zero-sum anchor)
            nets: dict[str, int] = {}
            for r, row in zip(day_recs, rows):
                net = _row_net(psp, row)
                nets[r["currency"]] = nets.get(r["currency"], 0) + net
            rows += _payout_rows(psp, day, nets, cols)
            df = pd.DataFrame(rows, columns=cols)
            if (psp, day) == withheld_file:
                dest = withheld / psp / f"{day}.csv"
                withheld_count += 1
            else:
                dest = psp_dir / psp / f"{day}.csv"
                written += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(dest, index=False)

    # persist orders for the ingest step
    orders_out = orders[["merchant_reference", "amount_minor", "currency", "psp"]].copy()
    orders_out["auth_date"] = orders["auth_date"].astype(str)
    orders_out.to_csv(outdir / "orders.csv", index=False)
    summary = {
        "orders": len(orders), "settlement_records": sum(len(v) for v in psp_recs.values()),
        "files_written": written, "files_withheld": withheld_count,
        "missing_legs": len(missing_leg_refs),
    }
    print("seed summary:", summary)
    return summary


def _row_net(psp: str, row: dict) -> int:
    if psp == "adyen":
        return int(row["Net (NC)"])
    if psp == "stripe":
        return int(Decimal(row["net"]) * 100)
    return int(Decimal(row["transaction_amount"]) * 100) - sum(
        int(Decimal(row[c]) * 100)
        for c in ("merchant_discount_fee", "buy_rate_fee", "rev_share_fee", "decline_fee")
    )


def _payout_rows(psp: str, day: str, nets: dict[str, int], cols: list[str]) -> list[dict]:
    rows = []
    for ccy, net in nets.items():
        if psp == "adyen":
            rows.append({
                "Company Account": "DemoCompany", "Merchant Account": "DemoMerchant",
                "Psp Reference": f"PAYOUT-{day}", "Merchant Reference": "",
                "Payment Method": "", "Creation Date": day, "TimeZone": "Europe/London",
                "Type": "", "Modification Reference": "", "Record Type": "PaidOut",
                "Gross Currency": ccy, "Gross (NC)": 0, "Net Currency": ccy,
                "Net (NC)": net, "Commission (NC)": 0, "Markup (NC)": 0,
                "Interchange (NC)": 0, "Scheme Fees (NC)": 0, "Exchange Rate": "1.0",
                "Batch Number": f"{psp}:{day}", "Batch Closed Date": day,
            })
        elif psp == "stripe":
            rows.append({
                "balance_transaction_id": f"po_{day}_{ccy}", "gross": "0.00",
                "fee": "0.00", "net": f"{net / 100:.2f}", "currency": ccy.lower(),
                "customer_facing_amount": "0.00", "customer_facing_currency": ccy.lower(),
                "exchange_rate": "1.0", "reporting_category": "payout",
                "payment_intent_id": "", "charge_id": "",
                "automatic_payout_id": f"po_{day}", "description": f"Payout {day}",
            })
        else:
            rows.append({
                "statement_date": day, "statement_id": f"STMT-{day}",
                "payment_date": day, "payment_id": f"PAYOUT-{day}-{ccy}",
                "mid": "DEMO-MID-001", "transaction_date": day,
                "transaction_time": "00:00:00", "transaction_amount": f"{net / 100:.2f}",
                "funds_status": "paid_out", "payment_currency": ccy,
                "merchant_discount_fee": "0.00", "buy_rate_fee": "0.00",
                "rev_share_fee": "0.00", "decline_fee": "0.00",
            })
    # pad every row to the full column set
    return [{c: r.get(c, "") for c in cols} for r in rows]


if __name__ == "__main__":
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")
    generate(outdir)
