"""
Omega ML — Machine Learning layer for trade signal classification and regime detection.

Modules:
    features       — 44-feature extraction from OHLCV candles
    neural_net     — Signal Quality Classifier (MLP binary classifier)
    regime         — Market Regime Detection (K-Means + rule-based)
    data_collector — Training data pipeline (Yahoo Finance + trade memory)
    engine         — Central ML engine integrating all models into Omega
"""
from ml.features import Candle, extract_features, FEATURE_NAMES
from ml.neural_net import SignalClassifier
from ml.regime import RegimeDetector, Regime, REGIME_NAMES
from ml.engine import OmegaMLEngine

__all__ = [
    "Candle",
    "extract_features",
    "FEATURE_NAMES",
    "SignalClassifier",
    "RegimeDetector",
    "Regime",
    "REGIME_NAMES",
    "OmegaMLEngine",
]
