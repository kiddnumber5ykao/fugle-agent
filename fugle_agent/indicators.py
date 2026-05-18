"""Pure-python technical indicators — no numpy/pandas dependency."""

from __future__ import annotations

from typing import Sequence


def sma(values: Sequence[float], window: int) -> list[float | None]:
    """Simple moving average. Returns same length, None for warm-up bars."""
    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float | None] = []
    running = 0.0
    buf: list[float] = []
    for v in values:
        buf.append(v)
        running += v
        if len(buf) > window:
            running -= buf.pop(0)
        out.append(running / window if len(buf) == window else None)
    return out


def ema(values: Sequence[float], window: int) -> list[float | None]:
    """Exponential moving average (Wilder-style smoothing seeded with SMA)."""
    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float | None] = []
    alpha = 2.0 / (window + 1.0)
    seed = None
    for i, v in enumerate(values):
        if i + 1 < window:
            out.append(None)
            continue
        if seed is None:
            seed = sum(values[: window]) / window
            out.append(seed)
            continue
        seed = alpha * v + (1 - alpha) * seed
        out.append(seed)
    return out


def rsi(values: Sequence[float], window: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) <= window:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, window + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / window
    avg_loss = losses / window
    rs = (avg_gain / avg_loss) if avg_loss else float("inf")
    out[window] = 100.0 - 100.0 / (1.0 + rs) if avg_loss else 100.0
    for i in range(window + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = max(diff, 0)
        loss = -min(diff, 0)
        avg_gain = (avg_gain * (window - 1) + gain) / window
        avg_loss = (avg_loss * (window - 1) + loss) / window
        rs = (avg_gain / avg_loss) if avg_loss else float("inf")
        out[i] = 100.0 - 100.0 / (1.0 + rs) if avg_loss else 100.0
    return out
