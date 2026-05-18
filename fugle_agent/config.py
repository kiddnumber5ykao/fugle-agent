"""Runtime configuration — read from environment variables.

Environment variables
---------------------
FUGLE_MARKETDATA_API_KEY   Fugle Market Data API key from https://developer.fugle.tw
FUGLE_MOCK                 "1" / "true" to force mock mode (default: auto — mock when no key)
FUGLE_RATE_LIMIT_SLEEP     Seconds to sleep between REST calls (default 0.25)
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    mock: bool
    rate_limit_sleep: float

    @classmethod
    def from_env(cls) -> "Settings":
        key = os.getenv("FUGLE_MARKETDATA_API_KEY") or os.getenv("FUGLE_API_KEY")
        forced_mock = _truthy(os.getenv("FUGLE_MOCK"))
        # auto-mock if no key supplied
        mock = forced_mock or not key
        try:
            sleep = float(os.getenv("FUGLE_RATE_LIMIT_SLEEP", "0.25"))
        except ValueError:
            sleep = 0.25
        return cls(api_key=key, mock=mock, rate_limit_sleep=sleep)


SETTINGS = Settings.from_env()
