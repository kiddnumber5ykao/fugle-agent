# ⬆️【要上傳 2026-06-04 22:32】us_market.py — 加美股最新資料時間(到分,台北)
"""US / global stock data + per-ticker news via yfinance.

Covers:
  - US stocks  (AAPL, MSFT, TSLA, NVDA, ...)
  - US ETFs    (SPY, QQQ, VOO, ...)
  - Crypto     (BTC-USD, ETH-USD)
  - Non-US listings (e.g. 2330.TW maps back to TSMC via Yahoo)

Data is delayed ~15-20 minutes (Yahoo standard).  yfinance is unofficial —
occasionally Yahoo rate-limits or temporarily blocks the API; we surface
those failures as ``{"error": "..."}`` so the agent can explain to the user.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

try:
    import yfinance as yf  # type: ignore
except Exception:  # pragma: no cover — import-time fallback for sandbox
    yf = None  # type: ignore


def _check() -> dict | None:
    if yf is None:
        return {"error": "yfinance 套件沒安裝 — 在 requirements.txt 加 yfinance 並重新部署"}
    return None


def quote(symbol: str) -> dict:
    err = _check()
    if err:
        return err
    try:
        ticker = yf.Ticker(symbol)  # type: ignore
        info = getattr(ticker, "fast_info", None)
        hist = ticker.history(period="5d", auto_adjust=False)
        if hist is None or hist.empty:
            return {"error": f"找不到 {symbol} 的資料"}
        last = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) > 1 else last
        prev_close = float(prev["Close"])
        last_close = float(last["Close"])
        change = last_close - prev_close
        change_pct = (change / prev_close * 100.0) if prev_close else 0.0
        try:
            long_name = ticker.info.get("longName") or ticker.info.get("shortName") or symbol  # type: ignore
        except Exception:
            long_name = symbol
        return {
            "symbol": symbol,
            "name": long_name,
            "exchange": getattr(info, "exchange", None) if info else None,
            "currency": getattr(info, "currency", "USD") if info else "USD",
            "previousClose": round(prev_close, 4),
            "openPrice": round(float(last["Open"]), 4),
            "highPrice": round(float(last["High"]), 4),
            "lowPrice":  round(float(last["Low"]), 4),
            "lastPrice": round(last_close, 4),
            "change":    round(change, 4),
            "changePercent": round(change_pct, 2),
            "volume":    int(last["Volume"]),
            "asOf":      str(last.name)[:19],   # date index
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def candles(symbol: str, *, from_date: str | None = None,
            to_date: str | None = None, interval: str = "1d") -> dict:
    err = _check()
    if err:
        return err
    try:
        if to_date is None:
            to_date = date.today().isoformat()
        if from_date is None:
            from_date = (date.today() - timedelta(days=180)).isoformat()
        ticker = yf.Ticker(symbol)  # type: ignore
        hist = ticker.history(start=from_date, end=to_date,
                              interval=interval, auto_adjust=False)
        if hist is None or hist.empty:
            return {"error": f"找不到 {symbol} 在 {from_date}..{to_date} 的資料"}
        bars: list[dict] = []
        for idx, row in hist.iterrows():
            bars.append({
                "date":   idx.strftime("%Y-%m-%d"),
                "open":   round(float(row["Open"]),  4),
                "high":   round(float(row["High"]),  4),
                "low":    round(float(row["Low"]),   4),
                "close":  round(float(row["Close"]), 4),
                "volume": int(row["Volume"]),
            })
        return {
            "symbol":   symbol,
            "interval": interval,
            "data":     bars,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def news(symbol: str, limit: int = 10) -> list[dict]:
    err = _check()
    if err:
        return [err]
    try:
        ticker = yf.Ticker(symbol)  # type: ignore
        items = list(getattr(ticker, "news", []) or [])
    except Exception as exc:  # noqa: BLE001
        return [{"error": f"{type(exc).__name__}: {exc}"}]

    out: list[dict] = []
    for it in items[:limit]:
        # yfinance v0.2.50+ wraps each news under "content"
        body = it.get("content", it) if isinstance(it, dict) else {}
        out.append({
            "title": body.get("title") or it.get("title"),
            "publisher": (
                body.get("provider", {}).get("displayName")
                if isinstance(body.get("provider"), dict) else None
            ) or it.get("publisher"),
            "link": (
                body.get("canonicalUrl", {}).get("url")
                if isinstance(body.get("canonicalUrl"), dict) else None
            ) or it.get("link"),
            "published": body.get("pubDate") or it.get("providerPublishTime"),
            "summary": body.get("summary", "") or it.get("summary", ""),
        })
    return out


def latest_index_time() -> str:
    """美股最近一筆「分鐘」資料的時間 → 轉台北 YYYY-MM-DD HH:MM。失敗回 ""。
    (yfinance 分鐘資料延遲約 15 分,所以這是延遲後的資料時間,但比只到「日」精準。)"""
    if yf is None:
        return ""
    try:
        h = yf.Ticker("^GSPC").history(period="1d", interval="1m")
        if h is None or h.empty:
            return ""
        ts = h.index[-1]
        try:
            ts = ts.tz_convert("Asia/Taipei")
        except Exception:
            pass
        return ts.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""
