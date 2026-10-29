"""
Omega ML — Machine Learning layer for trade signal classification and regime detection.

Modules:
    features       — 50-feature extraction from OHLCV candles
    neural_net     — Signal Quality Classifier (legacy MLP, kept for compatibility)
    sqlite_buffer  — SQLite-backed durable ML training buffer with counterfactual logging
    regime         — Market Regime Detection (K-Means + rule-based)
    data_collector — Training data pipeline (Yahoo Finance + trade memory)
    engine         — Central ML engine: LightGBM + SGD online adapter + SQLite buffer
"""
from ml.features import Candle, extract_features, FEATURE_NAMES
from ml.neural_net import SignalClassifier
from ml.sqlite_buffer import MLBuffer
from ml.regime import RegimeDetector, Regime, REGIME_NAMES
from ml.engine import OmegaMLEngine

__all__ = [
    "Candle",
    "extract_features",
    "FEATURE_NAMES",
    "SignalClassifier",
    "MLBuffer",
    "RegimeDetector",
    "Regime",
    "REGIME_NAMES",
    "OmegaMLEngine",
]
