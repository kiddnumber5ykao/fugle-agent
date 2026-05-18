"""Smoke tests — run with `python -m pytest tests/` or directly.

These tests run entirely against the deterministic mock client, so they
need neither Fugle credentials nor the `claude-agent-sdk` package.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# Force mock mode regardless of host env, BEFORE importing the package.
os.environ["FUGLE_MOCK"] = "1"

# Ensure the package is importable when running the file directly.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fugle_agent.client import FugleClient
from fugle_agent.indicators import ema, rsi, sma
from fugle_agent import backtest as bt
from fugle_agent import tools as agent_tools


def _decode(envelope: dict) -> dict:
    return json.loads(envelope["content"][0]["text"])


def _call(tool_obj, args: dict) -> dict:
    """Invoke a @tool-decorated handler regardless of whether the real
    claude-agent-sdk is installed (which wraps it in SdkMcpTool with a
    .handler attribute) or our fallback shim is in use."""
    handler = getattr(tool_obj, "handler", tool_obj)
    return asyncio.run(handler(args))


def test_client_mock_quote() -> None:
    c = FugleClient(force_mock=True)
    q = c.quote("2330")
    assert q["symbol"] == "2330"
    assert "lastPrice" in q
    assert q.get("isMock") is True


def test_client_mock_candles_have_180_days() -> None:
    c = FugleClient(force_mock=True)
    candles = c.candles("2330")
    bars = candles["data"]
    # ~180 calendar days but only weekdays — should land between 110 and 140.
    assert 100 <= len(bars) <= 160, f"unexpected bar count {len(bars)}"
    for b in bars[:5]:
        assert set(b) >= {"date", "open", "high", "low", "close", "volume"}


def test_indicators_smoke() -> None:
    series = list(range(1, 101))  # 1..100
    s20 = sma(series, 20)
    assert s20[18] is None and s20[19] is not None
    assert abs(s20[-1] - 90.5) < 1e-6  # mean of 81..100 = 90.5
    e10 = ema(series, 10)
    assert e10[8] is None and e10[9] is not None
    r = rsi(series, 14)
    assert r[14] == 100.0  # strictly increasing → RSI saturates at 100


def test_sma_crossover_backtest() -> None:
    c = FugleClient(force_mock=True)
    candles = c.candles("2330")
    result = bt.sma_crossover(candles["data"], fast=10, slow=30, symbol="2330")
    d = result.to_dict()
    assert d["symbol"] == "2330"
    assert d["n_bars"] == len(candles["data"])
    assert isinstance(d["total_return_pct"], float)
    assert isinstance(d["buy_hold_return_pct"], float)
    assert d["max_drawdown_pct"] <= 0.0


def test_rsi_meanrev_backtest() -> None:
    c = FugleClient(force_mock=True)
    candles = c.candles("2317")
    result = bt.rsi_mean_reversion(candles["data"], window=14, symbol="2317")
    d = result.to_dict()
    assert d["strategy"].startswith("rsi_meanrev")
    assert d["n_bars"] == len(candles["data"])


def test_tool_envelope_shape() -> None:
    out = _call(agent_tools.get_quote, {"symbol": "2330"})
    assert "content" in out and out["content"][0]["type"] == "text"
    body = _decode(out)
    assert body["mode"] == "mock"
    assert body["quote"]["symbol"] == "2330"


def test_tool_candles_summary() -> None:
    out = _call(agent_tools.get_candles, {"symbol": "2454"})
    body = _decode(out)
    assert body["symbol"] == "2454"
    assert body["n_bars"] > 0
    assert body["last"]["date"] >= body["first"]["date"]
    assert len(body["sample_tail"]) <= 10


def test_tool_indicators() -> None:
    out = _call(agent_tools.compute_indicators, {
        "symbol": "2330",
        "indicators": [{"kind": "sma", "window": 20}, {"kind": "rsi", "window": 14}],
        "tail": 5,
    })
    body = _decode(out)
    assert "sma_20" in body["indicators"]
    assert "rsi_14" in body["indicators"]
    assert len(body["tail_bars"]) == 5


def test_tool_backtest() -> None:
    out = _call(agent_tools.backtest_sma_crossover, {
        "symbol": "0050", "fast": 10, "slow": 30,
    })
    body = _decode(out)
    assert body["symbol"] == "0050"
    assert body["strategy"].startswith("sma_cross")


if __name__ == "__main__":
    import inspect
    failures = []
    tests = [(n, fn) for n, fn in globals().items() if n.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures.append((name, repr(e)))
            print(f"FAIL  {name}: {e}")
        else:
            print(f"ok    {name}")
    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        sys.exit(1)
    print(f"all {len(tests)} tests passed")
