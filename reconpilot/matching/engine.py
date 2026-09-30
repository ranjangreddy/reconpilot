"""Matching engine (PRD §5.3) — the deterministic core.

Three tiers, per Hyperswitch's reconciliation taxonomy:
  1. EXACT: merchant_reference + gross amount + currency.
  2. TOLERANT: merchant_reference + amount within FX-drift tolerance + date
     within the PSP's settlement-lag window (+ buffer). FX drift is booked
     to the FX variance account with the rate snapshot attached.
  3. ML-SCORED: the scorer proposes a probability over candidates; the
     ENGINE decides: >= threshold -> auto-match, >= suggest threshold ->
     exception WITH a suggested candidate, else -> plain exception.

Hard rule: below threshold, NEVER force a match. Every auto-match carries
tier + confidence + a machine-readable reason (auditability).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ..canonical import CanonicalRecord
from ..ledger.ledger import CREDIT, DEBIT, JournalLine, Ledger, PSP_CASH_ACCOUNT
from .scorer import MatchScorer, featurize


class MatchEngine:
    def __init__(self, ledger: Ledger, config: dict, scorer: MatchScorer):
        self.ledger = ledger
        self.cfg = config
        self.scorer = scorer
        m = config["matching"]
        self.ml_threshold = m["ml_threshold"]
        self.ml_suggest = m["ml_suggest_threshold"]
        self.date_buffer = m["date_buffer_days"]
        self.candidate_window = m["candidate_window_days"]

    # ---- tolerances ----
    def fx_tolerance(self, amount_minor: int) -> int:
        m = self.cfg["matching"]
        return max(
            int(abs(amount_minor) * m["fx_drift_tolerance_pct"]),
            m["fx_drift_tolerance_minor"],
        )

    # ---- main run ----
    def run(self) -> dict:
        stats = {
            "tier1": 0, "tier2": 0, "tier3": 0,
            "exceptions": 0, "total": 0, "already_matched": 0,
        }
        orders = self._open_orders()
        by_ref = {o["merchant_reference"]: o for o in orders}
        records = self._unmatched_records()
        stats["total"] = len(records)

        for rec in records:
            if rec["record_type"] != "payment":
                self._match_non_payment(rec, by_ref, stats)
                continue
            order = None
            tier = None
            confidence = 0.0
            reason = ""
            fx_variance = 0

            # Tier 1 — exact
            if rec["merchant_reference"]:
                cand = by_ref.get(rec["merchant_reference"])
                if (
                    cand
                    and cand["gross"] == rec["gross_amount"]
                    and cand["currency"] == rec["currency"]
                ):
                    order, tier, confidence = cand, 1, 1.0
                    reason = "exact: merchant_reference+amount+currency"
            # Tier 2 — tolerant
            if order is None and rec["merchant_reference"]:
                cand = by_ref.get(rec["merchant_reference"])
                if cand and cand["currency"] == rec["currency"]:
                    tol = self.fx_tolerance(cand["gross"])
                    lag = self.cfg["psps"][rec["psp"]]["settlement_lag_days"]
                    auth = self._d(cand["auth_date"])
                    settle = self._d(rec["settlement_date"])
                    if (
                        abs(cand["gross"] - rec["gross_amount"]) <= tol
                        and auth + timedelta(days=lag - 1)
                        <= settle
                        <= auth + timedelta(days=lag + self.date_buffer)
                    ):
                        order, tier = cand, 2
                        fx_variance = cand["gross"] - rec["gross_amount"]
                        confidence = round(
                            1.0
                            - abs(fx_variance) / max(tol, 1) * 0.2,
                            4,
                        )
                        reason = (
                            f"tolerant: ref match, amount drift {fx_variance} "
                            f"within tol {tol}, lag {lag}d"
                        )
            # Tier 3 — ML scored
            suggestion = ""
            if order is None:
                best, best_score, best_feat = self._best_candidate(rec, orders)
                if best is not None:
                    if best_score >= self.ml_threshold:
                        order, tier = best, 3
                        confidence = round(best_score, 4)
                        fx_variance = best["gross"] - rec["gross_amount"]
                        reason = (
                            f"ml: score {confidence} >= {self.ml_threshold}, "
                            f"drift {fx_variance}"
                        )
                    elif best_score >= self.ml_suggest:
                        suggestion = (
                            f"candidate {best['merchant_reference']} "
                            f"(score {round(best_score, 3)})"
                        )
            if order is not None:
                self._post_match(rec, order, tier, confidence, reason, fx_variance)
                stats[f"tier{tier}"] += 1
                # order consumed
                by_ref.pop(order["merchant_reference"], None)
            else:
                self.ledger.add_exception(
                    "ORPHAN_PAYMENT",
                    record_id=rec["record_id"],
                    psp=rec["psp"],
                    merchant_reference=rec["merchant_reference"],
                    currency=rec["currency"],
                    amount_minor=rec["gross_amount"],
                    detail=f"settlement with no matching order ({reason or 'no candidate'})",
                    suggestion=suggestion,
                )
                self._mark_record(rec, "exception", None, 0, 0.0, "orphan")
                stats["exceptions"] += 1

        # Missing legs: open orders past lag + buffer with no settlement
        stats["missing_legs"] = self._flag_missing_legs()
        stats["exceptions"] += stats["missing_legs"]
        return stats

    # ---- data access ----
    def _open_orders(self) -> list[dict]:
        # 'missing' is an alert state, not a terminal one: a late settlement
        # file must still be able to match (and heal) these orders.
        cur = self.ledger.db.execute(
            "SELECT merchant_reference, amount_minor AS gross, currency,"
            " auth_date, psp FROM orders WHERE status IN ('open', 'missing')"
        )
        return [dict(r) for r in cur.fetchall()]

    def _unmatched_records(self) -> list[dict]:
        cur = self.ledger.db.execute(
            "SELECT * FROM settlement_records WHERE match_status='unmatched'"
            " ORDER BY settlement_date"
        )
        return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def _d(s: str) -> date:
        return datetime.strptime(s, "%Y-%m-%d").date()

    # ---- tier 3 candidates ----
    def _best_candidate(self, rec: dict, orders: list[dict]):
        from difflib import SequenceMatcher

        settle = self._d(rec["settlement_date"])
        expected_lag = self.cfg["psps"][rec["psp"]]["settlement_lag_days"]
        rec_ref = rec["merchant_reference"] or ""
        scored = []
        for o in orders:
            if o["currency"] != rec["currency"]:
                continue
            # Guard: never ML-match two records that ASSERT different order
            # IDs. Ref-less settlement rows (Shift4-style) may match anything;
            # a "GHOST-0001" row may not steal order "51234".
            if rec_ref and o["merchant_reference"] != rec_ref:
                sim = SequenceMatcher(None, rec_ref, o["merchant_reference"]).ratio()
                if sim < 0.6:
                    continue
            auth = self._d(o["auth_date"])
            deviation = abs((settle - auth).days - expected_lag)
            if deviation > self.candidate_window:
                continue
            if abs(o["gross"] - rec["gross_amount"]) > max(
                o["gross"] * 0.05, 500
            ):
                continue
            feat = featurize(
                abs(o["gross"] - rec["gross_amount"]) / max(o["gross"], 1),
                deviation,
                o["merchant_reference"],
                rec["merchant_reference"],
                True,
                o["psp"] == rec["psp"],
                0.0,
            )
            scored.append((self.scorer.score(feat), o, feat))
        if not scored:
            return None, 0.0, None
        scored.sort(key=lambda t: -t[0])
        return scored[0][1], scored[0][0], scored[0][2]

    # ---- postings ----
    def _post_match(
        self,
        rec: dict,
        order: dict,
        tier: int,
        confidence: float,
        reason: str,
        fx_variance: int,
    ) -> None:
        """Reclass suspense -> cash-in-transit; book any FX drift to 5010.
        Always balanced by construction."""
        settle_gross = rec["gross_amount"]
        order_gross = order["gross"]
        lines = [
            JournalLine("1200", DEBIT, settle_gross, rec["currency"]),
            JournalLine("1100", CREDIT, order_gross, rec["currency"]),
        ]
        if fx_variance > 0:  # settled less than authorized -> loss
            lines.append(JournalLine("5010", DEBIT, fx_variance, rec["currency"]))
        elif fx_variance < 0:  # settled more -> gain
            lines.append(JournalLine("5010", CREDIT, -fx_variance, rec["currency"]))
        self.ledger.post_journal(
            lines,
            reference=f"match:{rec['record_id']}",
            batch_id=rec["batch_id"],
            idempotency_key=f"match:{rec['psp']}:{rec['record_id']}",
            txn_date=self._d(rec["settlement_date"]),
            description=f"tier{tier} match {rec['merchant_reference'] or rec['psp_reference']} "
            f"conf={confidence} :: {reason}",
        )
        self._mark_record(
            rec, "matched", order["merchant_reference"], tier, confidence, reason
        )
        self.ledger.db.execute(
            "UPDATE orders SET status='settled' WHERE merchant_reference=?",
            (order["merchant_reference"],),
        )
        self.ledger.db.commit()

    def _match_non_payment(self, rec: dict, by_ref: dict, stats: dict) -> None:
        """Refunds/chargebacks: match to the original order by reference and
        reverse revenue out of suspense. The original order is usually already
        settled, so look it up in the orders table directly (any status)."""
        order = None
        if rec["merchant_reference"]:
            cur = self.ledger.db.execute(
                "SELECT merchant_reference, amount_minor AS gross, currency,"
                " auth_date, psp FROM orders WHERE merchant_reference=?",
                (rec["merchant_reference"],),
            )
            row = cur.fetchone()
            if row:
                order = dict(row)
        if order is not None:
            amt = abs(rec["gross_amount"])
            self.ledger.post_journal(
                [
                    JournalLine("4000", DEBIT, amt, rec["currency"]),
                    JournalLine("1200", CREDIT, amt, rec["currency"]),
                ],
                reference=f"refund-match:{rec['record_id']}",
                batch_id=rec["batch_id"],
                idempotency_key=f"refundmatch:{rec['psp']}:{rec['record_id']}",
                txn_date=self._d(rec["settlement_date"]),
                description=f"refund of {order['merchant_reference']}",
            )
            self._mark_record(rec, "matched", order["merchant_reference"], 1, 1.0,
                              "refund: original order reference")
            stats["tier1"] += 1
        else:
            self.ledger.add_exception(
                "ORPHAN_PAYMENT",
                record_id=rec["record_id"],
                psp=rec["psp"],
                merchant_reference=rec["merchant_reference"],
                currency=rec["currency"],
                amount_minor=rec["gross_amount"],
                detail=f"{rec['record_type']} with no matching order",
            )
            self._mark_record(rec, "exception", None, 0, 0.0, "orphan non-payment")
            stats["exceptions"] += 1

    def _mark_record(self, rec, status, order_ref, tier, confidence, reason) -> None:
        self.ledger.db.execute(
            """UPDATE settlement_records SET match_status=?, matched_order_ref=?,
               match_tier=?, match_confidence=?, match_reason=? WHERE record_id=? AND psp=?""",
            (status, order_ref, tier, confidence, reason, rec["record_id"], rec["psp"]),
        )
        self.ledger.db.commit()

    def _flag_missing_legs(self) -> int:
        """Orders whose settlement never arrived (past lag + buffer).

        Only alerts once per order: already-'missing' orders keep their
        existing exception instead of getting a duplicate each run.
        """
        n = 0
        today = max(
            self._d(r["settlement_date"]) for r in self._unmatched_records()
        ) if self._unmatched_records() else date.today()
        cur = self.ledger.db.execute(
            "SELECT merchant_reference, amount_minor AS gross, currency,"
            " auth_date, psp FROM orders WHERE status='open'"
        )
        for o in [dict(r) for r in cur.fetchall()]:
            lag = self.cfg["psps"][o["psp"]]["settlement_lag_days"]
            if self._d(o["auth_date"]) + timedelta(days=lag + self.date_buffer) < today:
                self.ledger.add_exception(
                    "MISSING_PAYMENT",
                    psp=o["psp"],
                    merchant_reference=o["merchant_reference"],
                    currency=o["currency"],
                    amount_minor=o["gross"],
                    detail=f"order authorized {o['auth_date']} with no settlement "
                    f"after {lag + self.date_buffer}d window",
                    suggestion="check for missed settlement file / re-poll PSP",
                )
                self.ledger.db.execute(
                    "UPDATE orders SET status='missing' WHERE merchant_reference=?",
                    (o["merchant_reference"],),
                )
                n += 1
        self.ledger.db.commit()
        return n
