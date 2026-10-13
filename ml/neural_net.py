"""
Omega Signal Quality Classifier — Neural Network

Adapted from trekhleb/homemade-machine-learning MultilayerPerceptron.
Binary classifier: predicts WIN (1) or LOSS (0) from 44 trading features.

Architecture: 44 → 32 → 16 → 2  (input → hidden1 → hidden2 → output)

Changes from homemade-ML base:
  - Binary output with confidence score instead of multi-class argmax
  - He initialization instead of uniform random (better for ReLU-like activations)
  - Batch normalization approximation via z-score normalization
  - Early stopping on validation loss to prevent overfitting
  - Model serialization (save/load weights as JSON)
  - Online learning: incremental training on new trades
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).parent.parent / "data" / "ml" / "models"


def _sigmoid(z: np.ndarray) -> np.ndarray:
    z = np.clip(z, -500, 500)
    return 1.0 / (1.0 + np.exp(-z))


def _sigmoid_gradient(z: np.ndarray) -> np.ndarray:
    s = _sigmoid(z)
    return s * (1.0 - s)


def _normalize(data: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = np.mean(data, axis=0)
    std = np.std(data, axis=0)
    std[std == 0] = 1.0
    return (data - mean) / std, mean, std


class SignalClassifier:
    """Binary neural network classifier for trade signal quality."""

    def __init__(
        self,
        num_features: int = 44,
        hidden_layers: tuple[int, ...] = (32, 16),
        epsilon: float = 0.12,
    ):
        self.layers = [num_features, *hidden_layers, 2]  # 2 outputs: LOSS, WIN
        self.epsilon = epsilon
        self.thetas: dict[int, np.ndarray] = {}
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None
        self._trained = False
        self._cost_history: list[float] = []

        self._init_thetas()

    def _init_thetas(self) -> None:
        for i in range(len(self.layers) - 1):
            fan_in = self.layers[i]
            fan_out = self.layers[i + 1]
            limit = np.sqrt(6.0 / (fan_in + fan_out))
            self.thetas[i] = np.random.randn(fan_out, fan_in + 1) * limit

    def train(
        self,
        X: np.ndarray,
        y: np.ndarray,
        regularization: float = 0.1,
        alpha: float = 0.3,
        max_iterations: int = 2000,
        validation_split: float = 0.2,
        early_stop_patience: int = 50,
    ) -> list[float]:
        if len(X) < 10:
            log.warning("Need at least 10 samples to train, got %d", len(X))
            return []

        X_norm, self._mean, self._std = _normalize(X)

        num_examples = X_norm.shape[0]
        ones = np.ones((num_examples, 1))
        X_bias = np.hstack((ones, X_norm))

        y_int = y.astype(int).reshape(-1, 1)

        if validation_split > 0 and len(X) >= 20:
            split = int(len(X) * (1 - validation_split))
            idx = np.random.permutation(len(X))
            train_idx, val_idx = idx[:split], idx[split:]
            X_train, X_val = X_bias[train_idx], X_bias[val_idx]
            y_train, y_val = y_int[train_idx], y_int[val_idx]
        else:
            X_train, y_train = X_bias, y_int
            X_val, y_val = None, None

        self._init_thetas()

        unrolled = self._thetas_unroll()

        best_val_cost = float("inf")
        patience_counter = 0
        best_thetas = None

        self._cost_history = []

        for iteration in range(max_iterations):
            cost = self._cost_function(X_train, y_train, self._thetas_roll(unrolled), regularization)
            self._cost_history.append(cost)

            gradient = self._gradient_step(X_train, y_train, unrolled, regularization)
            unrolled = unrolled - alpha * gradient

            if X_val is not None and iteration % 10 == 0:
                val_cost = self._cost_function(X_val, y_val, self._thetas_roll(unrolled), 0.0)
                if val_cost < best_val_cost:
                    best_val_cost = val_cost
                    patience_counter = 0
                    best_thetas = unrolled.copy()
                else:
                    patience_counter += 1
                    if patience_counter >= early_stop_patience:
                        log.info("Early stopping at iteration %d (val_cost=%.4f)", iteration, best_val_cost)
                        unrolled = best_thetas
                        break

        self.thetas = self._thetas_roll(unrolled)
        if best_thetas is not None:
            self.thetas = self._thetas_roll(best_thetas)

        self._trained = True

        train_pred = self.predict(X)
        accuracy = np.mean((train_pred[:, 1] > 0.5).astype(int) == y.astype(int))
        log.info("Training complete: %d iterations, final_cost=%.4f, accuracy=%.2f%%",
                 len(self._cost_history), self._cost_history[-1], accuracy * 100)

        return self._cost_history

    def predict(self, X: np.ndarray) -> np.ndarray:
        if not self._trained or self._mean is None:
            return np.full((len(X), 2), 0.5)

        X_norm = (X - self._mean) / self._std
        num_examples = X_norm.shape[0]
        X_bias = np.hstack((np.ones((num_examples, 1)), X_norm))

        return self._feedforward(X_bias)

    def predict_confidence(self, features: np.ndarray) -> tuple[bool, float]:
        if features.ndim == 1:
            features = features.reshape(1, -1)

        probs = self.predict(features)
        win_prob = float(probs[0, 1])
        return win_prob > 0.5, win_prob

    def incremental_train(
        self,
        X: np.ndarray,
        y: np.ndarray,
        alpha: float = 0.01,
        iterations: int = 100,
        regularization: float = 0.1,
    ) -> None:
        if not self._trained or self._mean is None:
            self.train(X, y)
            return

        X_norm = (X - self._mean) / self._std
        num_examples = X_norm.shape[0]
        X_bias = np.hstack((np.ones((num_examples, 1)), X_norm))
        y_int = y.astype(int).reshape(-1, 1)

        unrolled = self._thetas_unroll()
        for _ in range(iterations):
            gradient = self._gradient_step(X_bias, y_int, unrolled, regularization)
            unrolled = unrolled - alpha * gradient

        self.thetas = self._thetas_roll(unrolled)

    def _feedforward(self, X: np.ndarray) -> np.ndarray:
        num_layers = len(self.layers)
        num_examples = X.shape[0]
        activation = X

        for i in range(num_layers - 1):
            theta = self.thetas[i]
            z = activation @ theta.T
            activation = _sigmoid(z)
            if i < num_layers - 2:
                activation = np.hstack((np.ones((num_examples, 1)), activation))

        return activation

    def _cost_function(
        self,
        X: np.ndarray,
        y: np.ndarray,
        thetas: dict[int, np.ndarray],
        reg: float,
    ) -> float:
        num_layers = len(self.layers)
        num_examples = X.shape[0]
        num_labels = self.layers[-1]

        activation = X
        for i in range(num_layers - 1):
            z = activation @ thetas[i].T
            activation = _sigmoid(z)
            if i < num_layers - 2:
                activation = np.hstack((np.ones((num_examples, 1)), activation))

        predictions = np.clip(activation, 1e-10, 1 - 1e-10)

        bitwise_labels = np.zeros((num_examples, num_labels))
        for j in range(num_examples):
            bitwise_labels[j, y[j, 0]] = 1

        cost = (-1 / num_examples) * (
            np.sum(bitwise_labels * np.log(predictions))
            + np.sum((1 - bitwise_labels) * np.log(1 - predictions))
        )

        if reg > 0:
            theta_sq = sum(np.sum(thetas[i][:, 1:] ** 2) for i in range(num_layers - 1))
            cost += (reg / (2 * num_examples)) * theta_sq

        return float(cost)

    def _gradient_step(
        self,
        X: np.ndarray,
        y: np.ndarray,
        unrolled: np.ndarray,
        reg: float,
    ) -> np.ndarray:
        thetas = self._thetas_roll(unrolled)
        grads = self._backprop(X, y, thetas, reg)
        return self._thetas_unroll_from(grads)

    def _backprop(
        self,
        X: np.ndarray,
        y: np.ndarray,
        thetas: dict[int, np.ndarray],
        reg: float,
    ) -> dict[int, np.ndarray]:
        num_layers = len(self.layers)
        num_examples, num_features = X.shape
        num_labels = self.layers[-1]

        deltas = {}
        for i in range(num_layers - 1):
            deltas[i] = np.zeros((self.layers[i + 1], self.layers[i] + 1))

        for ex in range(num_examples):
            layer_inputs = {}
            layer_activations = {}

            a = X[ex, :].reshape((num_features, 1))
            layer_activations[0] = a

            for i in range(num_layers - 1):
                z = thetas[i] @ a
                a = np.vstack((np.array([[1]]), _sigmoid(z)))
                layer_inputs[i + 1] = z
                layer_activations[i + 1] = a

            output = a[1:, :]

            delta = {}
            target = np.zeros((num_labels, 1))
            target[y[ex, 0]] = 1
            delta[num_layers - 1] = output - target

            for i in range(num_layers - 2, 0, -1):
                z = np.vstack((np.array([[1]]), layer_inputs[i]))
                delta[i] = (thetas[i].T @ delta[i + 1]) * _sigmoid_gradient(z)
                delta[i] = delta[i][1:, :]

            for i in range(num_layers - 1):
                deltas[i] += delta[i + 1] @ layer_activations[i].T

        for i in range(num_layers - 1):
            d = deltas[i]
            reg_term = np.hstack((np.zeros((d.shape[0], 1)), d[:, 1:]))
            deltas[i] = (1 / num_examples) * d + (reg / num_examples) * reg_term

        return deltas

    def _thetas_unroll(self) -> np.ndarray:
        return np.concatenate([self.thetas[i].flatten() for i in range(len(self.thetas))])

    def _thetas_unroll_from(self, thetas: dict[int, np.ndarray]) -> np.ndarray:
        return np.concatenate([thetas[i].flatten() for i in range(len(thetas))])

    def _thetas_roll(self, unrolled: np.ndarray) -> dict[int, np.ndarray]:
        thetas = {}
        offset = 0
        for i in range(len(self.layers) - 1):
            rows = self.layers[i + 1]
            cols = self.layers[i] + 1
            size = rows * cols
            thetas[i] = unrolled[offset:offset + size].reshape((rows, cols))
            offset += size
        return thetas

    def save(self, path: str | Path | None = None) -> Path:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        path = Path(path) if path else MODEL_DIR / "signal_classifier.json"

        data = {
            "layers": self.layers,
            "epsilon": self.epsilon,
            "trained": self._trained,
            "cost_history": self._cost_history[-100:],
            "mean": self._mean.tolist() if self._mean is not None else None,
            "std": self._std.tolist() if self._std is not None else None,
            "thetas": {
                str(k): v.tolist() for k, v in self.thetas.items()
            },
        }
        path.write_text(json.dumps(data, indent=2))
        log.info("Model saved to %s", path)
        return path

    @classmethod
    def load(cls, path: str | Path | None = None) -> SignalClassifier:
        path = Path(path) if path else MODEL_DIR / "signal_classifier.json"
        if not path.exists():
            log.warning("No saved model at %s, returning untrained", path)
            return cls()

        data = json.loads(path.read_text())
        model = cls(
            num_features=data["layers"][0],
            hidden_layers=tuple(data["layers"][1:-1]),
            epsilon=data["epsilon"],
        )
        model._trained = data["trained"]
        model._cost_history = data.get("cost_history", [])
        model._mean = np.array(data["mean"]) if data["mean"] else None
        model._std = np.array(data["std"]) if data["std"] else None
        model.thetas = {
            int(k): np.array(v) for k, v in data["thetas"].items()
        }
        model.layers = data["layers"]
        log.info("Model loaded from %s (trained=%s)", path, model._trained)
        return model
