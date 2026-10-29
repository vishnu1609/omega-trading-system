"""
Walk-Forward Purged Validation for Omega ML

Prevents temporal leakage by:
  1. Purge: removes training samples whose outcome window overlaps test start
  2. Embargo: additional gap after purge boundary (default 2 samples)
  3. Walk-forward: train→purge→embargo→test, slide forward

Uses LightGBM with reduced feature set to match production model exactly.

Reference: de Prado, "Advances in Financial Machine Learning" (2018), Ch. 7.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ml.features import FEATURE_NAMES, REDUCED_FEATURE_NAMES

log = logging.getLogger(__name__)

try:
    import lightgbm as lgb
    HAS_LGBM = True
except ImportError:
    HAS_LGBM = False

try:
    from sklearn.linear_model import SGDClassifier
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


@dataclass
class FoldResult:
    fold_id: int
    train_size: int
    test_size: int
    purged: int
    accuracy: float
    precision: float
    recall: float
    f1: float
    log_loss: float
    pred_win_rate: float
    actual_win_rate: float
    oos_preds: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))
    oos_labels: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))


@dataclass
class WalkForwardReport:
    folds: list[FoldResult]
    avg_accuracy: float = 0.0
    avg_f1: float = 0.0
    avg_log_loss: float = 0.0
    std_accuracy: float = 0.0
    overfit_ratio: float = 0.0  # in-sample / out-of-sample accuracy ratio

    def summary(self) -> str:
        return (
            f"WF-Purged: {len(self.folds)} folds | "
            f"OOS acc={self.avg_accuracy:.1f}%±{self.std_accuracy:.1f} | "
            f"F1={self.avg_f1:.3f} | "
            f"LogLoss={self.avg_log_loss:.4f} | "
            f"Overfit={self.overfit_ratio:.2f}x"
        )


def _reduce_features(X_full: np.ndarray) -> np.ndarray:
    """Extract reduced 15-feature subset from full feature vectors."""
    indices = []
    for rname in REDUCED_FEATURE_NAMES:
        if rname in FEATURE_NAMES:
            indices.append(FEATURE_NAMES.index(rname))
        else:
            indices.append(0)
    if X_full.shape[1] > max(indices):
        return X_full[:, indices]
    return X_full


def _compute_metrics(y_true: np.ndarray, y_pred_prob: np.ndarray) -> dict:
    """Compute classification metrics from probabilities."""
    y_pred = (y_pred_prob > 0.5).astype(int)
    y_int = y_true.astype(int)

    tp = int(np.sum((y_pred == 1) & (y_int == 1)))
    fp = int(np.sum((y_pred == 1) & (y_int == 0)))
    fn = int(np.sum((y_pred == 0) & (y_int == 1)))
    tn = int(np.sum((y_pred == 0) & (y_int == 0)))

    n = len(y_int)
    accuracy = (tp + tn) / max(n, 1)
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-10)

    eps = 1e-15
    clipped = np.clip(y_pred_prob, eps, 1 - eps)
    ll = -np.mean(y_int * np.log(clipped) + (1 - y_int) * np.log(1 - clipped))

    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "log_loss": ll,
        "pred_win_rate": float(np.mean(y_pred)),
        "actual_win_rate": float(np.mean(y_int)),
    }


def purged_walk_forward(
    X: np.ndarray,
    y: np.ndarray,
    timestamps: np.ndarray | None = None,
    train_window: int = 40,
    test_window: int = 10,
    step: int = 10,
    purge_gap: int = 3,
    embargo_gap: int = 2,
    lgbm_params: dict | None = None,
) -> WalkForwardReport:
    """Purged walk-forward validation matching production LightGBM.

    Args:
        X: full feature matrix (N, 51) — will be reduced to 15 internally
        y: labels (0=loss, 1=win)
        timestamps: optional Unix timestamps for time-aware purging
        train_window: number of samples in training window
        test_window: number of samples in test window
        step: slide step between folds
        purge_gap: samples to remove from end of training set
        embargo_gap: additional samples to skip after purge
        lgbm_params: override LightGBM params (uses production defaults if None)
    """
    n = len(X)
    min_needed = train_window + purge_gap + embargo_gap + test_window
    if n < min_needed:
        log.warning("Not enough data for purged WF: %d < %d needed", n, min_needed)
        return WalkForwardReport(folds=[])

    X_reduced = _reduce_features(X)

    if lgbm_params is None:
        lgbm_params = {
            "objective": "binary",
            "metric": "binary_logloss",
            "max_depth": 3,
            "num_leaves": 7,
            "learning_rate": 0.1,
            "min_child_samples": 3,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "reg_alpha": 0.1,
            "reg_lambda": 1.0,
            "verbose": -1,
            "seed": 42,
        }

    folds: list[FoldResult] = []
    in_sample_accs: list[float] = []

    for start in range(0, n - min_needed + 1, step):
        train_end = start + train_window
        purge_end = train_end + purge_gap
        embargo_end = purge_end + embargo_gap
        test_end = min(embargo_end + test_window, n)

        if test_end <= embargo_end:
            continue

        X_train = X_reduced[start:train_end]
        y_train = y[start:train_end]
        X_test = X_reduced[embargo_end:test_end]
        y_test = y[embargo_end:test_end]

        if len(np.unique(y_train)) < 2 or len(y_test) < 2:
            continue

        purged_count = purge_gap + embargo_gap

        if HAS_LGBM:
            params = lgbm_params.copy()
            params["min_child_samples"] = max(3, len(y_train) // 10)
            n_rounds = min(100, max(20, len(y_train) * 2))

            train_data = lgb.Dataset(
                X_train, label=y_train, free_raw_data=False,
                feature_name=REDUCED_FEATURE_NAMES,
            )
            try:
                model = lgb.train(params, train_data, num_boost_round=n_rounds)
            except Exception as e:
                log.warning("Fold %d LightGBM failed: %s", len(folds), e)
                continue

            oos_probs = model.predict(X_test)
            is_probs = model.predict(X_train)

        elif HAS_SKLEARN:
            scaler = StandardScaler()
            X_tr_scaled = scaler.fit_transform(X_train)
            X_te_scaled = scaler.transform(X_test)
            sgd = SGDClassifier(loss="log_loss", penalty="l2", alpha=0.001,
                                max_iter=200, random_state=42)
            sgd.fit(X_tr_scaled, y_train)
            oos_probs = sgd.predict_proba(X_te_scaled)[:, 1]
            is_probs = sgd.predict_proba(X_tr_scaled)[:, 1]
        else:
            log.warning("No ML library available for validation")
            return WalkForwardReport(folds=[])

        oos_metrics = _compute_metrics(y_test, oos_probs)
        is_metrics = _compute_metrics(y_train, is_probs)
        in_sample_accs.append(is_metrics["accuracy"])

        result = FoldResult(
            fold_id=len(folds),
            train_size=len(y_train),
            test_size=len(y_test),
            purged=purged_count,
            accuracy=oos_metrics["accuracy"],
            precision=oos_metrics["precision"],
            recall=oos_metrics["recall"],
            f1=oos_metrics["f1"],
            log_loss=oos_metrics["log_loss"],
            pred_win_rate=oos_metrics["pred_win_rate"],
            actual_win_rate=oos_metrics["actual_win_rate"],
            oos_preds=oos_probs,
            oos_labels=y_test,
        )
        folds.append(result)

        log.info(
            "Fold %d: train=%d test=%d purged=%d | OOS acc=%.1f%% f1=%.3f ll=%.4f | IS acc=%.1f%%",
            result.fold_id, result.train_size, result.test_size, purged_count,
            result.accuracy * 100, result.f1, result.log_loss,
            is_metrics["accuracy"] * 100,
        )

    if not folds:
        return WalkForwardReport(folds=[])

    accs = np.array([f.accuracy for f in folds])
    avg_is_acc = np.mean(in_sample_accs) if in_sample_accs else 0
    avg_oos_acc = float(np.mean(accs))

    report = WalkForwardReport(
        folds=folds,
        avg_accuracy=avg_oos_acc * 100,
        avg_f1=float(np.mean([f.f1 for f in folds])),
        avg_log_loss=float(np.mean([f.log_loss for f in folds])),
        std_accuracy=float(np.std(accs)) * 100,
        overfit_ratio=avg_is_acc / max(avg_oos_acc, 0.01),
    )

    log.info("=== PURGED WALK-FORWARD SUMMARY ===")
    log.info(report.summary())
    if report.overfit_ratio > 1.3:
        log.warning("HIGH OVERFIT RATIO: %.2fx — model may not generalize", report.overfit_ratio)

    return report


def validate_from_buffer(buffer, **kwargs) -> WalkForwardReport:
    """Run purged walk-forward using data from the SQLite buffer."""
    X_list, y_list = buffer.get_training_data(min_samples=15)
    if not X_list:
        log.warning("No training data in buffer for validation")
        return WalkForwardReport(folds=[])

    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.int32)

    kwargs.setdefault("train_window", max(20, len(y) // 3))
    kwargs.setdefault("test_window", max(5, len(y) // 10))
    kwargs.setdefault("step", max(3, len(y) // 15))

    log.info("Validating on %d samples (train_w=%d, test_w=%d, step=%d)",
             len(y), kwargs["train_window"], kwargs["test_window"], kwargs["step"])

    return purged_walk_forward(X, y, **kwargs)


def feature_importance(
    X: np.ndarray,
    y: np.ndarray,
    n_repeats: int = 5,
) -> list[tuple[str, float]]:
    """Permutation-based feature importance using production LightGBM."""
    X_reduced = _reduce_features(X)

    if not HAS_LGBM:
        log.warning("LightGBM not available for feature importance")
        return []

    params = {
        "objective": "binary", "metric": "binary_logloss",
        "max_depth": 3, "num_leaves": 7, "learning_rate": 0.1,
        "verbose": -1, "seed": 42,
    }
    train_data = lgb.Dataset(X_reduced, label=y, free_raw_data=False,
                             feature_name=REDUCED_FEATURE_NAMES)
    model = lgb.train(params, train_data, num_boost_round=50)

    preds_base = model.predict(X_reduced)
    base_acc = np.mean((preds_base > 0.5).astype(int) == y.astype(int))

    importances = []
    for i, name in enumerate(REDUCED_FEATURE_NAMES):
        drops = []
        for _ in range(n_repeats):
            X_perm = X_reduced.copy()
            np.random.shuffle(X_perm[:, i])
            preds_perm = model.predict(X_perm)
            perm_acc = np.mean((preds_perm > 0.5).astype(int) == y.astype(int))
            drops.append(base_acc - perm_acc)
        importances.append((name, float(np.mean(drops))))

    importances.sort(key=lambda x: x[1], reverse=True)
    log.info("Feature importance (reduced set):")
    for name, imp in importances[:10]:
        log.info("  %s: %.4f", name, imp)

    return importances
