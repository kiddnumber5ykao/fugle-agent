"""Deterministic mock data generator — used when no Fugle API key is configured.

The mock client mirrors the response shape of fugle-marketdata so tools can be
written once and work in both modes.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

# A handful of representative Taiwan-listed symbols for the mock universe.
_SYMBOL_PROFILES: dict[str, dict] = {
    "2330": {"name": "台積電", "base": 1000.0, "vol_scale": 25_000_000},
    "2317": {"name": "鴻海",   "base": 200.0,  "vol_scale": 40_000_000},
    "2454": {"name": "聯發科", "base": 1300.0, "vol_scale": 8_000_000},
    "2603": {"name": "長榮",   "base": 220.0,  "vol_scale": 60_000_000},
    "0050": {"name": "元大台灣50", "base": 180.0, "vol_scale": 12_000_000},
    "IX0001": {"name": "發行量加權股價指數", "base": 22000.0, "vol_scale": 0},
}


def _profile(symbol: str) -> dict:
    if symbol in _SYMBOL_PROFILES:
        return _SYMBOL_PROFILES[symbol]
    # Deterministic synthetic profile for unknown symbols.
    h = int(hashlib.sha1(symbol.encode()).hexdigest(), 16)
    base = 20.0 + (h % 10_000) / 10.0          # 20 – 1020
    vol = 1_000_000 + (h % 50) * 500_000
    return {"name": f"Mock-{symbol}", "base": base, "vol_scale": vol}


def _rng(symbol: str, salt: str = "") -> random.Random:
    seed = int(hashlib.sha1(f"{symbol}|{salt}".encode()).hexdigest(), 16) & 0xFFFFFFFF
    return random.Random(seed)


def _daily_series(symbol: str, start: date, end: date) -> list[dict]:
    """Generate a deterministic daily OHLCV series for symbol between start..end."""
    profile = _profile(symbol)
    rng = _rng(symbol, "daily")
    out: list[dict] = []
    price = profile["base"]
    cursor = start
    drift = 0.0005  # tiny upward drift
    vol_scale = profile["vol_scale"] or 1_000_000
    while cursor <= end:
        if cursor.weekday() < 5:  # skip weekends (TW market closed)
            change = rng.gauss(drift, 0.012)
            new_price = max(1.0, price * (1.0 + change))
            high = max(price, new_price) * (1.0 + abs(rng.gauss(0, 0.004)))
            low = min(price, new_price) * (1.0 - abs(rng.gauss(0, 0.004)))
            open_p = price
            close_p = new_price
            volume = max(1, int(vol_scale * (0.5 + rng.random())))
            out.append({
                "date": cursor.isoformat(),
                "open": round(open_p, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(close_p, 2),
                "volume": volume,
            })
            price = new_price
        cursor += timedelta(days=1)
    return out


class MockRestClient:
    """Drop-in replacement for ``fugle_marketdata.RestClient`` (subset)."""

    def __init__(self) -> None:
        self.stock = _StockNamespace()


class _StockNamespace:
    def __init__(self) -> None:
        self.intraday = _IntradayNamespace()
        self.historical = _HistoricalNamespace()
        self.snapshot = _SnapshotNamespace()


class _HistoricalNamespace:
    def candles(self, *, symbol: str, **kwargs) -> dict:
        # accept both `from_` (python keyword work-around) and `from`
        start_s = kwargs.get("from_") or kwargs.get("from") or (date.today() - timedelta(days=30)).isoformat()
        end_s = kwargs.get("to") or date.today().isoformat()
        timeframe = kwargs.get("timeframe", "D")
        start = date.fromisoformat(start_s)
        end = date.fromisoformat(end_s)
        return {
            "symbol": symbol,
            "type": "EQUITY",
            "exchange": "TWSE",
            "market": "TSE",
            "timeframe": timeframe,
            "data": _daily_series(symbol, start, end),
        }


class _IntradayNamespace:
    def quote(self, *, symbol: str, **_) -> dict:
        profile = _profile(symbol)
        rng = _rng(symbol, "quote")
        last = profile["base"] * (1.0 + rng.gauss(0, 0.01))
        prev_close = profile["base"]
        change = last - prev_close
        return {
            "symbol": symbol,
            "name": profile["name"],
            "exchange": "TWSE",
            "market": "TSE",
            "previousClose": round(prev_close, 2),
            "openPrice": round(prev_close * (1.0 + rng.gauss(0, 0.003)), 2),
            "highPrice": round(max(prev_close, last) * (1.0 + abs(rng.gauss(0, 0.004))), 2),
            "lowPrice":  round(min(prev_close, last) * (1.0 - abs(rng.gauss(0, 0.004))), 2),
            "lastPrice": round(last, 2),
            "change": round(change, 2),
            "changePercent": round((change / prev_close) * 100.0, 2),
            "total": {"tradeVolume": int(profile["vol_scale"] * (0.4 + rng.random()))},
            "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "isMock": True,
        }

    def trades(self, *, symbol: str, limit: int = 50, **_) -> dict:
        profile = _profile(symbol)
        rng = _rng(symbol, "trades")
        base = profile["base"]
        now = datetime.now(timezone.utc)
        rows: list[dict] = []
        for i in range(limit):
            price = round(base * (1.0 + rng.gauss(0, 0.003)), 2)
            ts = now - timedelta(seconds=i * 3)
            rows.append({
                "bid": round(price - 0.5, 2),
                "ask": round(price + 0.5, 2),
                "price": price,
                "size": rng.randint(1, 50),
                "time": int(ts.timestamp() * 1_000_000_000),
                "serial": 10_000 - i,
                "tickType": rng.choice([1, 2]),  # 1 = buy, 2 = sell
            })
        return {"symbol": symbol, "data": rows, "isMock": True}


class _SnapshotNamespace:
    def movers(self, *, market: str = "TSE", direction: str = "up", **_) -> dict:
        rng = _rng(market, f"movers-{direction}")
        sign = 1 if direction == "up" else -1
        out = []
        for sym, prof in _SYMBOL_PROFILES.items():
            if sym.startswith("IX"):
                continue
            pct = sign * (5 + rng.random() * 5)
            out.append({
                "symbol": sym,
                "name": prof["name"],
                "lastPrice": round(prof["base"] * (1 + pct / 100), 2),
                "changePercent": round(pct, 2),
            })
        out.sort(key=lambda r: r["changePercent"], reverse=(direction == "up"))
        return {"market": market, "direction": direction, "data": out, "isMock": True}


def known_symbols() -> Iterable[str]:
    return _SYMBOL_PROFILES.keys()
