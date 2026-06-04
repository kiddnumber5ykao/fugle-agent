"""Thin wrapper over fugle-marketdata + an automatic mock fallback.

The wrapper exposes a tiny façade so the tool layer doesn't have to care
whether it's hitting Fugle live or the deterministic mock.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

from .config import SETTINGS
from .mock import MockRestClient


class FugleClient:
    """Façade around `fugle_marketdata.RestClient` with mock fallback."""

    def __init__(self, *, force_mock: bool | None = None) -> None:
        self._mock = SETTINGS.mock if force_mock is None else force_mock
        self._sleep = SETTINGS.rate_limit_sleep
        self._client: Any
        if self._mock:
            self._client = MockRestClient()
        else:
            # Import lazily so mock mode doesn't require the SDK installed.
            from fugle_marketdata import RestClient  # type: ignore

            self._client = RestClient(api_key=SETTINGS.api_key)

    # ---------- introspection ----------
    @property
    def mode(self) -> str:
        return "mock" if self._mock else "live"

    # ---------- internal ----------
    def _throttle(self) -> None:
        if not self._mock and self._sleep:
            time.sleep(self._sleep)

    # ---------- public API ----------
    def quote(self, symbol: str) -> dict:
        self._throttle()
        return self._client.stock.intraday.quote(symbol=symbol)

    def candles(
        self,
        symbol: str,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        timeframe: str = "D",
    ) -> dict:
        self._throttle()
        if to_date is None:
            to_date = date.today().isoformat()
        if from_date is None:
            from_date = (date.today() - timedelta(days=180)).isoformat()
        # `from` is a Python keyword — the SDK accepts `from_`; the mock
        # accepts either.  We pass both for forward-compat.
        kwargs = {"symbol": symbol, "to": to_date, "timeframe": timeframe, "from_": from_date}
        return self._client.stock.historical.candles(**kwargs)

    def intraday_ticks(self, symbol: str, *, limit: int = 50) -> dict:
        self._throttle()
        return self._client.stock.intraday.trades(symbol=symbol, limit=limit)

    def intraday_candles(self, symbol: str, *, timeframe: str = "1") -> dict:
        """盤中分鐘 K(給「此刻燈」用)。timeframe='1' 是 1 分鐘。
        不同 Fugle SDK 版本介面略有出入,且免費方案不一定開放 → 全 try,
        失敗回 {} 讓上層自動退回用逐筆(ticks)或報價(quote)。"""
        self._throttle()
        try:
            return self._client.stock.intraday.candles(symbol=symbol, timeframe=timeframe)
        except TypeError:
            try:
                return self._client.stock.intraday.candles(symbol=symbol)
            except Exception:
                return {}
        except Exception:
            return {}

    def movers(self, *, market: str = "TSE", direction: str = "up") -> dict:
        self._throttle()
        return self._client.stock.snapshot.movers(market=market, direction=direction)

    # ---- tickers list:給 symbol_lookup 用的「全市場代號+名稱」 ------
    _tickers_cache: list[dict] | None = None  # 進程內快取,避免每次都打網路

    def tickers(self) -> list[dict]:
        """回傳一份扁平化的 [{"symbol": "...", "name": "..."}] 清單。

        Live 模式抓 Fugle 的 intraday tickers(EQUITY + ETF + INDEX),
        失敗回空 list。Mock 模式回 mock 內建的少量股票。
        """
        if self._tickers_cache is not None:
            return self._tickers_cache

        out: list[dict] = []
        if self._mock:
            # Mock 模式給一個極簡 list,符合測試需求
            out = [
                {"symbol": "2330", "name": "台積電"},
                {"symbol": "0050", "name": "元大台灣50"},
                {"symbol": "IX0001", "name": "加權指數"},
            ]
        else:
            # 嘗試多種 type;不同 Fugle SDK 版本介面略有出入,用 try 一一吃
            for ticker_type in ("EQUITY", "ETF", "INDEX"):
                try:
                    data = self._client.stock.intraday.tickers(type=ticker_type)
                except Exception:
                    continue
                rows = data.get("data") if isinstance(data, dict) else None
                if not rows:
                    continue
                for r in rows:
                    sym = str(r.get("symbol") or "").strip()
                    name = str(r.get("name") or r.get("nameZhTw") or "").strip()
                    if sym and name:
                        out.append({"symbol": sym, "name": name})

        self._tickers_cache = out
        return out
