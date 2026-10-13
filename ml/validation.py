"""
Walk-Forward Validation and Feature Importance for Omega ML

Prevents overfitting by:
  1. Walk-forward: train on window, test on next period, slide forward
  2. Feature importance: permutation-based importance ranking
  3. Performance tracking: logs accuracy/sharpe per training window

Inspired by Advanced-Deep-Trading and intelligent-trading-bot repos.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ml.features import FEATURE_NAMES
from ml.neural_net import SignalClassifier

log = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    window_id: int
    train_size: int
    test_size: int
    accuracy: float
    precision: float
    recall: float
    f1: float
    win_rate_pred: float
    win_rate_actual: float


def walk_forward_validate(
    X: np.ndarray,
    y: np.ndarray,
    train_window: int = 50,
    test_window: int = 10,
    step: int = 10,
    hidden_layers: tuple[int, ...] = (32, 16),
    regularization: float = 0.3,
    max_iterations: int = 1500,
) -> list[ValidationResult]:
    """
    Walk-forward validation: train on [i:i+train_window], test on [i+train_window:i+train_window+test_window].
    Slide by `step` each iteration. Mirrors how the model would be retrained in production.
    """
    n = len(X)
    if n < train_window + test_window:
        log.warning("Not enough data: %d < %d needed", n, train_window + test_window)
        return []

    results = []
    window_id = 0

    for start in range(0, n - train_window - test_window + 1, step):
        train_end = start + train_window
        test_end = min(train_end + test_window, n)

        X_train, y_train = X[start:train_end], y[start:train_end]
        X_test, y_test = X[train_end:test_end], y[train_end:test_end]

        if len(np.unique(y_train)) < 2:
            continue

        clf = SignalClassifier(num_features=X.shape[1], hidden_layers=hidden_layers)
        clf.train(
            X_train, y_train,
            regularization=regularization,
            max_iterations=max_iterations,
            validation_split=0.0,
        )

        preds = clf.predict(X_test)
        pred_labels = (preds[:, 1] > 0.5).astype(int)
        actual = y_test.astype(int)

        tp = int(np.sum((pred_labels == 1) & (actual == 1)))
        fp = int(np.sum((pred_labels == 1) & (actual == 0)))
        fn = int(np.sum((pred_labels == 0) & (actual == 1)))
        tn = int(np.sum((pred_labels == 0) & (actual == 0)))

        accuracy = (tp + tn) / max(len(actual), 1)
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-10)

        result = ValidationResult(
            window_id=window_id,
            train_size=len(X_train),
            test_size=len(X_test),
            accuracy=accuracy,
            precision=precision,
            recall=recall,
            f1=f1,
            win_rate_pred=float(np.mean(pred_labels)),
            win_rate_actual=float(np.mean(actual)),
        )
        results.append(result)
        window_id += 1

        log.info(
            "Window %d: train=%d test=%d acc=%.1f%% prec=%.1f%% rec=%.1f%% f1=%.3f",
            window_id, len(X_train), len(X_test),
            accuracy * 100, precision * 100, recall * 100, f1,
        )

    if results:
        avg_acc = np.mean([r.accuracy for r in results])
        avg_f1 = np.mean([r.f1 for r in results])
        log.info("Walk-forward summary: %d windows, avg_acc=%.1f%%, avg_f1=%.3f",
                 len(results), avg_acc * 100, avg_f1)

    return results


def feature_importance(
    X: np.ndarray,
    y: np.ndarray,
    n_repeats: int = 5,
    hidden_layers: tuple[int, ...] = (32, 16),
) -> list[tuple[str, float]]:
    """
    Permutation-based feature importance. For each feature:
      1. Train model once on original data
      2. Shuffle that feature column n_repeats times
      3. Measure accuracy drop → importance score
    """
    clf = SignalClassifier(num_features=X.shape[1], hidden_layers=hidden_layers)
    clf.train(X, y, max_iterations=1500, validation_split=0.0)

    preds_baseline = clf.predict(X)
    baseline_acc = np.mean((preds_baseline[:, 1] > 0.5).astype(int) == y.astype(int))

    importances = []

    for i, feat_name in enumerate(FEATURE_NAMES[:X.shape[1]]):
        drops = []
        for _ in range(n_repeats):
            X_perm = X.copy()
            np.random.shuffle(X_perm[:, i])
            preds_perm = clf.predict(X_perm)
            perm_acc = np.mean((preds_perm[:, 1] > 0.5).astype(int) == y.astype(int))
            drops.append(baseline_acc - perm_acc)
        importances.append((feat_name, float(np.mean(drops))))

    importances.sort(key=lambda x: x[1], reverse=True)

    log.info("Top 10 features by importance:")
    for name, imp in importances[:10]:
        log.info("  %s: %.4f", name, imp)

    return importances
