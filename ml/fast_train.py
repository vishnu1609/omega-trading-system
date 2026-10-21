"""
Fast vectorized training — no per-example loops.

Uses mini-batch SGD with fully vectorized forward/backward passes.
Trains on 20K+ samples in minutes instead of hours.
"""
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from ml.features import FEATURE_NAMES, features_to_array
from ml.regime import RegimeDetector

log = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent.parent / "data" / "ml"
MODEL_DIR = DATA_DIR / "models"


def sigmoid(z):
    z = np.clip(z, -500, 500)
    return 1.0 / (1.0 + np.exp(-z))


class FastClassifier:
    """Vectorized neural network — no per-example loops."""

    def __init__(self, layer_sizes):
        self.layers = layer_sizes
        self.weights = {}
        self.biases = {}
        self._mean = None
        self._std = None

        for i in range(len(layer_sizes) - 1):
            fan_in = layer_sizes[i]
            fan_out = layer_sizes[i + 1]
            limit = np.sqrt(2.0 / fan_in)
            self.weights[i] = np.random.randn(fan_in, fan_out) * limit
            self.biases[i] = np.zeros((1, fan_out))

    def forward(self, X):
        activations = [X]
        a = X
        for i in range(len(self.layers) - 1):
            z = a @ self.weights[i] + self.biases[i]
            a = sigmoid(z)
            activations.append(a)
        return activations

    def backward(self, activations, y_onehot, reg):
        m = len(y_onehot)
        n_layers = len(self.layers)
        grads_w = {}
        grads_b = {}

        delta = activations[-1] - y_onehot

        for i in range(n_layers - 2, -1, -1):
            grads_w[i] = (activations[i].T @ delta) / m + (reg / m) * self.weights[i]
            grads_b[i] = np.mean(delta, axis=0, keepdims=True)

            if i > 0:
                delta = (delta @ self.weights[i].T) * activations[i] * (1 - activations[i])

        return grads_w, grads_b

    def train(self, X, y, lr=0.1, reg=0.1, epochs=200, batch_size=512,
              lr_decay=0.999, val_split=0.1, patience=30):
        # Normalize
        self._mean = np.mean(X, axis=0)
        self._std = np.std(X, axis=0)
        self._std[self._std == 0] = 1.0
        X_norm = (X - self._mean) / self._std

        # One-hot encode
        y_int = y.astype(int)
        y_onehot = np.zeros((len(y), self.layers[-1]))
        y_onehot[np.arange(len(y)), y_int] = 1.0

        # Train/val split
        perm = np.random.permutation(len(X))
        split = int(len(X) * (1 - val_split))
        train_idx, val_idx = perm[:split], perm[split:]
        X_train, y_train = X_norm[train_idx], y_onehot[train_idx]
        X_val, y_val_oh = X_norm[val_idx], y_onehot[val_idx]
        y_val_labels = y_int[val_idx]

        best_val_acc = 0
        best_weights = None
        best_biases = None
        wait = 0

        for epoch in range(epochs):
            # Shuffle
            idx = np.random.permutation(len(X_train))
            X_train = X_train[idx]
            y_train = y_train[idx]

            # Mini-batches
            for start in range(0, len(X_train), batch_size):
                X_b = X_train[start:start + batch_size]
                y_b = y_train[start:start + batch_size]

                acts = self.forward(X_b)
                gw, gb = self.backward(acts, y_b, reg)

                for i in range(len(self.layers) - 1):
                    self.weights[i] -= lr * gw[i]
                    self.biases[i] -= lr * gb[i]

            # Decay LR
            lr *= lr_decay

            # Validation
            if epoch % 5 == 0:
                val_acts = self.forward(X_val)
                val_pred = np.argmax(val_acts[-1], axis=1)
                val_acc = np.mean(val_pred == y_val_labels) * 100

                train_acts = self.forward(X_train)
                train_loss = -np.mean(y_train * np.log(np.clip(train_acts[-1], 1e-10, 1)))

                if epoch % 20 == 0:
                    print(f"  Epoch {epoch:3d}: loss={train_loss:.4f} val_acc={val_acc:.1f}% lr={lr:.5f}")

                if val_acc > best_val_acc:
                    best_val_acc = val_acc
                    best_weights = {k: v.copy() for k, v in self.weights.items()}
                    best_biases = {k: v.copy() for k, v in self.biases.items()}
                    wait = 0
                else:
                    wait += 1
                    if wait >= patience:
                        print(f"  Early stop at epoch {epoch} (best val_acc={best_val_acc:.1f}%)")
                        break

        if best_weights:
            self.weights = best_weights
            self.biases = best_biases

        return best_val_acc

    def predict(self, X):
        if self._mean is None:
            return np.full((len(X), self.layers[-1]), 0.5)
        X_norm = (X - self._mean) / self._std
        acts = self.forward(X_norm)
        return acts[-1]

    def predict_confidence(self, features):
        if features.ndim == 1:
            features = features.reshape(1, -1)
        probs = self.predict(features)
        win_prob = float(probs[0, 1])
        return win_prob > 0.5, win_prob

    def save(self, path=None):
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        path = Path(path) if path else MODEL_DIR / "signal_classifier.json"

        # Save in the SAME format as SignalClassifier so the bot can load it
        # Convert to theta format: theta[i] = (fan_out, fan_in+1) with bias in col 0
        thetas = {}
        for i in range(len(self.layers) - 1):
            W = self.weights[i]  # (fan_in, fan_out)
            b = self.biases[i]   # (1, fan_out)
            # SignalClassifier theta format: (fan_out, fan_in+1)
            theta = np.hstack([b.T, W.T])  # bias as first column
            thetas[str(i)] = theta.tolist()

        data = {
            "layers": self.layers,
            "epsilon": 0.12,
            "trained": True,
            "cost_history": [],
            "mean": self._mean.tolist() if self._mean is not None else None,
            "std": self._std.tolist() if self._std is not None else None,
            "thetas": thetas,
        }
        path.write_text(json.dumps(data, indent=2))
        print(f"  Model saved to {path}")
        return path

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path else MODEL_DIR / "signal_classifier.json"
        if not path.exists():
            return cls([50, 64, 32, 16, 2])
        data = json.loads(path.read_text())
        model = cls(data["layers"])
        model._mean = np.array(data["mean"]) if data["mean"] else None
        model._std = np.array(data["std"]) if data["std"] else None
        for k, v in data["thetas"].items():
            i = int(k)
            theta = np.array(v)  # (fan_out, fan_in+1)
            model.biases[i] = theta[:, 0:1].T  # (1, fan_out)
            model.weights[i] = theta[:, 1:].T   # (fan_in, fan_out)
        return model


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("Loading dataset...")
    with open(DATA_DIR / "mega_dataset.json") as f:
        data = json.load(f)

    X_list, y_list = [], []
    for entry in data:
        features = entry["features"]
        if isinstance(features, list):
            if len(features) >= len(FEATURE_NAMES):
                features = {name: float(features[i]) for i, name in enumerate(FEATURE_NAMES)}
            else:
                continue
        if not isinstance(features, dict):
            continue
        arr = features_to_array(features, FEATURE_NAMES)
        X_list.append(arr)
        y_list.append(float(entry["label"]))

    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.float32)
    print(f"Loaded: {len(X)} samples ({int(y.sum())}W / {int(len(y)-y.sum())}L)")

    # Architecture: 50 → 64 → 32 → 16 → 2
    print("\nTraining 50 → 64 → 32 → 16 → 2...")
    t0 = time.time()
    model = FastClassifier([X.shape[1], 64, 32, 16, 2])
    best_val_acc = model.train(
        X, y,
        lr=0.15,
        reg=0.1,
        epochs=300,
        batch_size=512,
        lr_decay=0.998,
        patience=40,
    )
    elapsed = time.time() - t0
    print(f"\nTraining: {elapsed:.0f}s")

    # Full accuracy
    preds = model.predict(X)
    pred_labels = np.argmax(preds, axis=1)
    acc = np.mean(pred_labels == y.astype(int)) * 100

    tp = np.sum((pred_labels == 1) & (y == 1))
    fp = np.sum((pred_labels == 1) & (y == 0))
    tn = np.sum((pred_labels == 0) & (y == 0))
    fn = np.sum((pred_labels == 0) & (y == 1))
    prec = tp / (tp + fp) * 100 if (tp + fp) else 0
    rec = tp / (tp + fn) * 100 if (tp + fn) else 0

    print(f"Full accuracy: {acc:.1f}%")
    print(f"Precision: {prec:.1f}%, Recall: {rec:.1f}%")
    print(f"TP={tp} FP={fp} TN={tn} FN={fn}")

    model.save()

    # Regime detector
    print("\nTraining regime detector...")
    feature_dicts = [e["features"] for e in data[:2000] if isinstance(e["features"], dict)]
    if feature_dicts:
        detector = RegimeDetector()
        detector.train(feature_dicts)
        detector.save()
        print("  Regime detector saved")

    print("\n✓ Training complete!")


if __name__ == "__main__":
    main()
