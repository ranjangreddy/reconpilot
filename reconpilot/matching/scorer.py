"""Match scorer (PRD §5.7): a supervised classifier that PROPOSES match
probabilities. Deterministic rules (the engine) always decide the booking.

Bootstrapped now (not deferred): trained on true pairs with injected noise
(amount jitter, date jitter, reference typos — including no-reference
positives for Shift4-style files) plus random negative pairs. Every
resolution a human makes is appended to the training set for retraining.
"""
from __future__ import annotations

import csv
import pickle
import random
from difflib import SequenceMatcher
from pathlib import Path

FEATURES = [
    "amount_delta_pct",
    "date_delta_days",
    "ref_similarity",
    "same_currency",
    "same_psp",
    "fee_delta_pct",
]


def featurize(
    amount_delta_pct: float,
    date_delta_days: float,
    ref_a: str,
    ref_b: str,
    same_currency: bool,
    same_psp: bool,
    fee_delta_pct: float,
) -> list[float]:
    ref_sim = (
        SequenceMatcher(None, ref_a or "", ref_b or "").ratio()
        if (ref_a or ref_b)
        else 0.0
    )
    return [
        min(amount_delta_pct, 1.0),
        min(date_delta_days, 30.0) / 30.0,
        ref_sim,
        1.0 if same_currency else 0.0,
        1.0 if same_psp else 0.0,
        min(fee_delta_pct, 1.0),
    ]


def _typo(s: str, rng: random.Random) -> str:
    if not s or rng.random() > 0.25:
        return s
    s = list(s)
    i = rng.randrange(len(s))
    s[i] = rng.choice("0123456789")
    return "".join(s)


def generate_training_data(n_pos: int = 3000, n_neg: int = 3000, seed: int = 42):
    """Synthetic-but-shaped training pairs. Positives mirror the faults the
    engine must survive: FX jitter, settlement-date jitter, reference typos,
    and Shift4-style pairs with no merchant reference at all."""
    rng = random.Random(seed)
    rows: list[tuple[list[float], int]] = []
    psps = ["adyen", "stripe", "shift4"]
    ccys = ["GBP", "EUR", "USD"]
    for _ in range(n_pos):
        amount = rng.randint(500, 50000)
        no_ref = rng.random() < 0.25  # Shift4-style: no merchant reference
        ref = "" if no_ref else f"5{rng.randint(10000, 99999)}"
        jittered_amount = int(amount * (1 + rng.uniform(-0.004, 0.004)))
        date_delta = abs(rng.gauss(0, 1.5))
        rows.append(
            (
                featurize(
                    abs(jittered_amount - amount) / amount,
                    date_delta,
                    ref,
                    _typo(ref, rng),
                    True,
                    True,
                    abs(rng.gauss(0, 0.02)),
                ),
                1,
            )
        )
    for _ in range(n_neg):
        a1, a2 = rng.randint(500, 50000), rng.randint(500, 50000)
        r1 = f"5{rng.randint(10000, 99999)}"
        r2 = f"5{rng.randint(10000, 99999)}"
        rows.append(
            (
                featurize(
                    abs(a1 - a2) / max(a1, 1),
                    rng.uniform(0, 20),
                    r1,
                    r2,
                    rng.random() < 0.7,
                    rng.random() < 0.5,
                    rng.uniform(0, 0.5),
                ),
                0,
            )
        )
    rng.shuffle(rows)
    X = [r[0] for r in rows]
    y = [r[1] for r in rows]
    return X, y


class MatchScorer:
    def __init__(self, model_path: str | Path = "models/match_scorer.pkl"):
        self.model_path = Path(model_path)
        self.model = None
        if self.model_path.exists():
            with open(self.model_path, "rb") as f:
                self.model = pickle.load(f)

    def train(self, X, y) -> dict:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import precision_score, recall_score
        from sklearn.model_selection import train_test_split

        Xtr, Xte, ytr, yte = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y
        )
        self.model = LogisticRegression(max_iter=1000, random_state=42)
        self.model.fit(Xtr, ytr)
        pred = self.model.predict(Xte)
        metrics = {
            "precision": round(float(precision_score(yte, pred)), 4),
            "recall": round(float(recall_score(yte, pred)), 4),
            "n_train": len(Xtr),
        }
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.model_path, "wb") as f:
            pickle.dump(self.model, f)
        return metrics

    def bootstrap(self) -> dict:
        X, y = generate_training_data()
        return self.train(X, y)

    def score(self, features: list[float]) -> float:
        if self.model is None:
            raise RuntimeError("scorer not trained — call bootstrap() first")
        return float(self.model.predict_proba([features])[0][1])

    def append_resolution(self, features: list[float], label: int,
                          path: str | Path = "data/resolutions.csv") -> None:
        """Every human resolution becomes training data (PRD §5.5)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(FEATURES + ["label"])
            w.writerow([*features, label])

    def retrain_with_resolutions(self, path: str | Path = "data/resolutions.csv") -> dict | None:
        path = Path(path)
        if not path.exists():
            return None
        with open(path) as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return None
        X = [[float(r[c]) for c in FEATURES] for r in rows]
        y = [int(r["label"]) for r in rows]
        Xb, yb = generate_training_data()
        return self.train(Xb + X, yb + y)
