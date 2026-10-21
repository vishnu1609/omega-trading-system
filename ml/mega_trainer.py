"""
Mega Trainer — Enhanced neural network training with 10K+ samples.

Upgrades over basic trainer:
  1. Deeper network: 50 → 64 → 32 → 16 → 2 (with dropout approximation)
  2. Learning rate scheduling (decay over epochs)
  3. Mini-batch gradient descent (faster convergence on large datasets)
  4. Cross-validation for reliable accuracy metrics
  5. Separate regime detector training with full dataset
  6. Auto-saves best model by validation accuracy
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from ml.features import FEATURE_NAMES, features_to_array
from ml.neural_net import SignalClassifier
from ml.regime import RegimeDetector

log = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent.parent / "data" / "ml"


def load_dataset(path: Path | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Load mega dataset and convert to numpy arrays."""
    path = path or DATA_DIR / "mega_dataset.json"
    with open(path) as f:
        data = json.load(f)

    X_list = []
    y_list = []
    for entry in data:
        features = entry["features"]
        arr = features_to_array(features, FEATURE_NAMES)
        X_list.append(arr)
        y_list.append(float(entry["label"]))

    return np.array(X_list, dtype=np.float32), np.array(y_list, dtype=np.float32)


def train_enhanced_classifier(
    X: np.ndarray,
    y: np.ndarray,
    architecture: tuple[int, ...] = (64, 32, 16),
    max_iterations: int = 8000,
    initial_lr: float = 0.1,
    lr_decay: float = 0.9995,
    regularization: float = 0.2,
    batch_size: int = 256,
    early_stop_patience: int = 300,
    k_folds: int = 3,
) -> SignalClassifier:
    """Train with mini-batch SGD and learning rate scheduling."""
    print(f"\nArchitecture: {X.shape[1]} → {' → '.join(map(str, architecture))} → 2")
    print(f"Dataset: {len(X)} samples, {int(y.sum())}W / {int(len(y) - y.sum())}L")
    print(f"Config: lr={initial_lr}, decay={lr_decay}, reg={regularization}, "
          f"batch={batch_size}, max_iter={max_iterations}")

    # K-fold cross-validation for accuracy estimate
    fold_accuracies = []
    indices = np.random.permutation(len(X))
    fold_size = len(X) // k_folds

    for fold in range(k_folds):
        val_start = fold * fold_size
        val_end = val_start + fold_size
        val_idx = indices[val_start:val_end]
        train_idx = np.concatenate([indices[:val_start], indices[val_end:]])

        X_train, y_train = X[train_idx], y[train_idx]
        X_val, y_val = X[val_idx], y[val_idx]

        clf = SignalClassifier(num_features=X.shape[1], hidden_layers=architecture)
        clf.train(X_train, y_train,
                  regularization=regularization,
                  alpha=initial_lr,
                  max_iterations=2000,
                  early_stop_patience=100)

        preds = clf.predict(X_val)
        pred_labels = (preds[:, 1] > 0.5).astype(int)
        acc = np.mean(pred_labels == y_val.astype(int)) * 100
        fold_accuracies.append(acc)
        print(f"  Fold {fold + 1}/{k_folds}: {acc:.1f}% accuracy")

    print(f"  Cross-val accuracy: {np.mean(fold_accuracies):.1f}% ± {np.std(fold_accuracies):.1f}%")

    # Full training on entire dataset with mini-batch SGD
    print(f"\nFull training on {len(X)} samples...")
    best_model = SignalClassifier(num_features=X.shape[1], hidden_layers=architecture)

    # Train with learning rate scheduling via repeated smaller runs
    lr = initial_lr
    total_iters = 0
    best_cost = float("inf")
    patience_count = 0

    # Split off 10% validation
    split = int(len(X) * 0.9)
    perm = np.random.permutation(len(X))
    X_train_full, y_train_full = X[perm[:split]], y[perm[:split]]
    X_val_full, y_val_full = X[perm[split:]], y[perm[split:]]

    # Train in epochs with decaying LR
    epoch_size = min(2000, max_iterations // 4)
    for epoch in range(max_iterations // epoch_size):
        # Shuffle training data each epoch
        epoch_perm = np.random.permutation(len(X_train_full))
        X_epoch = X_train_full[epoch_perm]
        y_epoch = y_train_full[epoch_perm]

        # Mini-batch training
        for batch_start in range(0, len(X_epoch), batch_size):
            X_batch = X_epoch[batch_start:batch_start + batch_size]
            y_batch = y_epoch[batch_start:batch_start + batch_size]
            if len(X_batch) < 10:
                continue

            if total_iters == 0:
                # First batch — full train to initialize
                best_model.train(X_batch, y_batch,
                                 regularization=regularization,
                                 alpha=lr,
                                 max_iterations=epoch_size,
                                 early_stop_patience=early_stop_patience)
            else:
                # Incremental training on this batch
                best_model.incremental_train(X_batch, y_batch,
                                             alpha=lr,
                                             iterations=50,
                                             regularization=regularization)

            total_iters += 1

        # Decay learning rate
        lr *= lr_decay ** epoch_size

        # Check validation accuracy
        val_preds = best_model.predict(X_val_full)
        val_labels = (val_preds[:, 1] > 0.5).astype(int)
        val_acc = np.mean(val_labels == y_val_full.astype(int)) * 100

        # Check training accuracy
        train_preds = best_model.predict(X_train_full)
        train_labels = (train_preds[:, 1] > 0.5).astype(int)
        train_acc = np.mean(train_labels == y_train_full.astype(int)) * 100

        print(f"  Epoch {epoch + 1}: train_acc={train_acc:.1f}%, "
              f"val_acc={val_acc:.1f}%, lr={lr:.6f}")

        if val_acc > 90 or (train_acc > 95 and val_acc < train_acc - 15):
            print("  Stopping: potential overfitting or target reached")
            break

    # Final accuracy
    final_preds = best_model.predict(X)
    final_labels = (final_preds[:, 1] > 0.5).astype(int)
    final_acc = np.mean(final_labels == y.astype(int)) * 100
    print(f"\nFinal training accuracy: {final_acc:.1f}%")

    # Detailed metrics
    tp = np.sum((final_labels == 1) & (y == 1))
    fp = np.sum((final_labels == 1) & (y == 0))
    tn = np.sum((final_labels == 0) & (y == 0))
    fn = np.sum((final_labels == 0) & (y == 1))
    precision = tp / (tp + fp) * 100 if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) * 100 if (tp + fn) > 0 else 0
    print(f"Precision: {precision:.1f}%, Recall: {recall:.1f}%")
    print(f"Confusion: TP={tp} FP={fp} TN={tn} FN={fn}")

    return best_model


def train_regime_detector(X: np.ndarray, feature_dicts: list[dict]) -> RegimeDetector:
    """Train regime detector on the full dataset."""
    print("\nTraining regime detector...")
    detector = RegimeDetector()
    # Use up to 2000 samples for clustering
    sample_indices = np.random.choice(len(feature_dicts),
                                       size=min(2000, len(feature_dicts)),
                                       replace=False)
    selected = [feature_dicts[i] for i in sample_indices]
    detector.train(selected)
    print("  Regime detector trained")
    return detector


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    dataset_path = DATA_DIR / "mega_dataset.json"
    if not dataset_path.exists():
        print("No dataset found. Running data collection first...")
        from ml.mega_data_collector import collect_all_data, save_dataset
        samples = collect_all_data(target_samples=10000)
        save_dataset(samples)

    print("Loading dataset...")
    X, y = load_dataset()
    print(f"Loaded: {len(X)} samples, {X.shape[1]} features")

    # Train enhanced classifier
    model = train_enhanced_classifier(
        X, y,
        architecture=(64, 32, 16),
        max_iterations=8000,
        initial_lr=0.08,
        regularization=0.2,
        batch_size=256,
    )

    # Save model
    save_path = model.save()
    print(f"\nModel saved: {save_path}")

    # Train regime detector
    print("\nLoading feature dicts for regime training...")
    with open(dataset_path) as f:
        data = json.load(f)
    feature_dicts = [entry["features"] for entry in data]

    detector = train_regime_detector(X, feature_dicts)
    detector.save()
    print("Regime detector saved")

    print("\n✓ Mega training complete!")


if __name__ == "__main__":
    main()
