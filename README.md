# ReconPilot — payment reconciliation engine

**Gr4vy got you to 99.9% authorization. This closes the books.**

Payment orchestrators route transactions across PSPs to maximize authorization
rates. But routing multiplies settlement reports: each PSP pays out on its own
schedule, in its own format, with its own fee math. Finance teams reconcile it
all by hand — downloading CSVs every Friday and stitching them in spreadsheets.

ReconPilot is the deterministic core of that reconciliation: ingest three
PSPs' real settlement formats into one double-entry ledger, match every
settlement to its authorization through three tiers (exact → tolerant → ML),
auto-heal the routine failures, and produce a gated month-end close pack.

```
 ┌─────────┐  ┌─────────┐  ┌─────────┐
 │  Adyen  │  │ Stripe  │  │ Shift4  │   real public column specs
 └────┬────┘  └────┬────┘  └────┬────┘
      └────────┬───┴──────┬─────┘
               ▼          ▼
        ┌─────────────┐  zero-sum integrity gate
        │   parsers   │  (quarantine on violation)
        └──────┬──────┘
               ▼  canonical records (minor units, signed)
        ┌─────────────┐  Dr Cash / Dr Fees / Cr Suspense
        │   ingest    │  file + record idempotency
        └──────┬──────┘
               ▼
        ┌─────────────┐  tier 1 exact → tier 2 tolerant (FX→5010)
        │   matching  │  → tier 3 ML-proposed, rules-disposed
        └──────┬──────┘
               ▼
        ┌─────────────┐  missed files → re-poll → resolve
        │  auto-heal  │  delay / fee-creep anomaly alerts
        └──────┬──────┘
               ▼
        ┌─────────────┐  balance gate → fee variance → cash
        │  close pack │  forecast → CSV + HTML report
        └─────────────┘
```

The one rule the whole system obeys: **AI proposes, rules dispose.** The ML
scorer returns probabilities; deterministic tiers decide every booking. Nothing
nondeterministic ever touches the ledger.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
make seed    # UCI Online Retail II -> orders.parquet + 3 PSP file sets (+ faults)
make demo    # the 90-second demo: ingest -> match -> heal -> close pack
make test    # integrity, idempotency, match-rate (>=95%)
```

`make demo` needs the seed data first (the 45MB source xlsx is downloaded once;
`data/` is gitignored). Postgres is optional: set `DATABASE_URL` and
`docker compose up -d` — SQLite is the default and the demo runs on it.

## The 90-second demo (`demo.py`)

1. **Ingest** ~3.7k real authorizations (UCI Online Retail II, Nov 2010) and the
   three PSP settlement file sets. One batch is adversarial: a withheld Adyen
   file, FX drift on ~2% of Stripe records, within-file duplicates, one Stripe
   day billed at 2× the contracted fee, orphan settlements, and orders that
   never settle.
2. **Match** — three tiers report the auto-match rate (target ≥ 95%).
3. **Auto-heal** — the missed file is detected from the authorization schedule,
   re-polled from the PSP, matched, and its exceptions auto-resolved.
4. **Close pack** — the balance gate runs, fee variance vs the rate card is
   printed per PSP, and `reports/close-pack-2011-01.html` is generated.

## Workbench

```bash
.venv/bin/python -m reconpilot.workbench.cli exceptions   # the queue
.venv/bin/python -m reconpilot.workbench.cli show 12      # the evidence
.venv/bin/python -m reconpilot.workbench.cli resolve 12 --to 51234   # match it
.venv/bin/python -m reconpilot.workbench.cli resolve 13 --writeoff    # or eat it
```

Every manual match becomes training data — the scorer retrains, the ledger
postings stay deterministic.

## Design notes

- **Canonical model** (`reconpilot/canonical.py`): all amounts in minor units,
  refunds/chargebacks signed negative, the money identity `net == gross − fee`
  asserted on every record. FX drift is modeled as *settlement gross ≠ order
  gross* so the identity stays a pure PSP-arithmetic check; drift books to the
  FX variance account (5010) at match time.
- **Shift4 carries no merchant reference** in its public spec — those records
  intentionally exercise the tolerant + ML tiers, the way a real merchant's
  worst-format file would. The matcher refuses to ML-match two records that
  assert *different* order IDs (a `GHOST-0001` row can never steal order
  `51234`).
- **Seed data is real-shaped, not synthetic.** Authorizations come from the UCI
  Online Retail II dataset (Nov 2010: ~2.6k invoices after filtering). This
  copy encodes cancellations as C-prefix invoices whose originals predate the
  demo window, so refunds are *modeled* as full refunds on ~1.5% of real
  orders (real refs, amounts, dates, PSPs). All other faults are injected
  deterministically (seed 42).
- **Parsers are built to the PSPs' public column specs** (Adyen settlement
  details, Stripe payout reconciliation, Shift4 settlement statement v1.5).
  One documented affordance: Stripe's report scopes rows by payout interval
  parameters, so the per-file date comes from the daily filename.

## Deviations from the PRD (all deliberate)

- **No Docker in this environment** → the ledger backend defaults to SQLite
  (`data/reconpilot.db`); Postgres via `DATABASE_URL` + `docker-compose.yml`
  is documented for real deployments.
- **Third parser is Shift4, not Checkout.com** — Shift4 publishes a full
  settlement-statement spec; no clean public field spec for Checkout.com
  surfaced.
- **Close gate reports blockers instead of raising** — `assert_balanced`
  returns the per-currency imbalances so the HTML close pack can itemize them.
