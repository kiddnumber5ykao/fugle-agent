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

    def movers(self, *, market: str = "TSE", direction: str = "up") -> dict:
        self._throttle()
        return self._client.stock.snapshot.movers(market=market, direction=direction)
