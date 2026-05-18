"""Claude Agent SDK tool definitions.

Each tool returns the SDK-expected envelope:

    {"content": [{"type": "text", "text": <json-string>}]}

We serialize structured data as JSON inside the text block so the LLM can read
the field names directly.  Tools are intentionally read-only — this agent does
NOT place orders.
"""

from __future__ import annotations

import json
from typing import Any

try:  # the SDK is only required when running the real agent — tests can skip
    from claude_agent_sdk import tool  # type: ignore
except Exception:  # pragma: no cover — fallback for unit tests / mock-only use
    from dataclasses import dataclass

    @dataclass
    class _FallbackTool:
        """Mimics claude_agent_sdk.SdkMcpTool so tests and agent code stay uniform."""
        name: str
        description: str
        input_schema: dict
        handler: object  # the original async function

    def tool(name: str, description: str, schema: dict):  # type: ignore
        def deco(fn):
            return _FallbackTool(name=name, description=description,
                                 input_schema=schema, handler=fn)
        return deco

from . import backtest as bt
from .client import FugleClient
from .indicators import ema, rsi, sma

# Single client per process — picks mock vs live from env.
_client = FugleClient()


def _envelope(data: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}]}


# ---------- quote / snapshot ----------

@tool(
    "get_quote",
    "Get the latest intraday snapshot (last price, change, volume) for a Taiwan-listed symbol. "
    "Use Taiwan ticker codes like '2330' (TSMC), '2317' (Hon Hai), '0050' (ETF), or 'IX0001' (TAIEX index).",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Taiwan stock ticker, e.g. '2330'."},
        },
        "required": ["symbol"],
    },
)
async def get_quote(args: dict) -> dict:
    data = _client.quote(args["symbol"])
    return _envelope({"mode": _client.mode, "quote": data})


# ---------- historical candles ----------

@tool(
    "get_candles",
    "Get historical daily OHLCV candles for a Taiwan-listed symbol. Default range = last 180 days. "
    "Returns a list of bars with date, open, high, low, close, volume.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "from_date": {"type": "string", "description": "ISO date 'YYYY-MM-DD', optional."},
            "to_date": {"type": "string", "description": "ISO date 'YYYY-MM-DD', optional."},
            "timeframe": {"type": "string", "description": "D / W / M (default D)", "default": "D"},
        },
        "required": ["symbol"],
    },
)
async def get_candles(args: dict) -> dict:
    data = _client.candles(
        args["symbol"],
        from_date=args.get("from_date"),
        to_date=args.get("to_date"),
        timeframe=args.get("timeframe", "D"),
    )
    # Trim payload — the LLM only needs summary stats, not every bar.
    bars = data.get("data", [])
    summary = {
        "symbol": data.get("symbol"),
        "timeframe": data.get("timeframe"),
        "mode": _client.mode,
        "n_bars": len(bars),
        "first": bars[0] if bars else None,
        "last": bars[-1] if bars else None,
        "sample_tail": bars[-10:],
    }
    return _envelope(summary)


# ---------- intraday ticks ----------

@tool(
    "get_intraday_ticks",
    "Get the most recent intraday trades (ticks) for a Taiwan-listed symbol — useful for "
    "near-realtime monitoring.  `limit` defaults to 30 ticks.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "limit": {"type": "integer", "default": 30, "minimum": 1, "maximum": 200},
        },
        "required": ["symbol"],
    },
)
async def get_intraday_ticks(args: dict) -> dict:
    data = _client.intraday_ticks(args["symbol"], limit=int(args.get("limit", 30)))
    return _envelope({"mode": _client.mode, **data})


# ---------- movers ----------

@tool(
    "get_market_movers",
    "Top gainers / losers in TSE or OTC, sorted by today's percent change.",
    {
        "type": "object",
        "properties": {
            "market": {"type": "string", "enum": ["TSE", "OTC"], "default": "TSE"},
            "direction": {"type": "string", "enum": ["up", "down"], "default": "up"},
        },
        "required": [],
    },
)
async def get_market_movers(args: dict) -> dict:
    data = _client.movers(
        market=args.get("market", "TSE"),
        direction=args.get("direction", "up"),
    )
    return _envelope({"mode": _client.mode, **data})


# ---------- indicators ----------

@tool(
    "compute_indicators",
    "Compute one or more technical indicators (sma, ema, rsi) on the daily close prices of a symbol "
    "over a date range.  Returns the indicator values for the LAST `tail` bars (default 30).",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "from_date": {"type": "string"},
            "to_date": {"type": "string"},
            "indicators": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["sma", "ema", "rsi"]},
                        "window": {"type": "integer"},
                    },
                    "required": ["kind", "window"],
                },
            },
            "tail": {"type": "integer", "default": 30},
        },
        "required": ["symbol", "indicators"],
    },
)
async def compute_indicators(args: dict) -> dict:
    data = _client.candles(args["symbol"],
                           from_date=args.get("from_date"),
                           to_date=args.get("to_date"))
    bars = data.get("data", [])
    closes = [b["close"] for b in bars]
    tail = int(args.get("tail", 30))
    series: dict[str, list] = {}
    for spec in args["indicators"]:
        kind = spec["kind"].lower()
        w = int(spec["window"])
        if kind == "sma":
            series[f"sma_{w}"] = sma(closes, w)
        elif kind == "ema":
            series[f"ema_{w}"] = ema(closes, w)
        elif kind == "rsi":
            series[f"rsi_{w}"] = rsi(closes, w)
        else:
            raise ValueError(f"Unknown indicator kind: {kind}")
    bars_tail = bars[-tail:]
    out_series = {k: v[-tail:] for k, v in series.items()}
    return _envelope({
        "mode": _client.mode,
        "symbol": args["symbol"],
        "n_bars": len(bars),
        "tail_bars": bars_tail,
        "indicators": out_series,
    })


# ---------- backtests ----------

@tool(
    "backtest_sma_crossover",
    "Long-only SMA crossover backtest: long when fast SMA > slow SMA, flat otherwise. "
    "Trades execute at next bar's open; default fee 5 bps each side.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "from_date": {"type": "string"},
            "to_date": {"type": "string"},
            "fast": {"type": "integer", "default": 20},
            "slow": {"type": "integer", "default": 60},
            "fee_bps": {"type": "number", "default": 5},
        },
        "required": ["symbol"],
    },
)
async def backtest_sma_crossover(args: dict) -> dict:
    data = _client.candles(args["symbol"],
                           from_date=args.get("from_date"),
                           to_date=args.get("to_date"))
    bars = data.get("data", [])
    if len(bars) < int(args.get("slow", 60)) + 5:
        return _envelope({"error": "not enough bars for the requested windows",
                          "n_bars": len(bars)})
    result = bt.sma_crossover(
        bars,
        fast=int(args.get("fast", 20)),
        slow=int(args.get("slow", 60)),
        fee_bps=float(args.get("fee_bps", 5)),
        symbol=args["symbol"],
    )
    return _envelope({"mode": _client.mode, **result.to_dict()})


@tool(
    "backtest_rsi_mean_reversion",
    "Long-only RSI mean-reversion backtest: enter long when RSI < lower, exit when RSI > upper. "
    "Trades execute at next bar's open; default fee 5 bps each side.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "from_date": {"type": "string"},
            "to_date": {"type": "string"},
            "window": {"type": "integer", "default": 14},
            "lower": {"type": "number", "default": 30},
            "upper": {"type": "number", "default": 55},
            "fee_bps": {"type": "number", "default": 5},
        },
        "required": ["symbol"],
    },
)
async def backtest_rsi_mean_reversion(args: dict) -> dict:
    data = _client.candles(args["symbol"],
                           from_date=args.get("from_date"),
                           to_date=args.get("to_date"))
    bars = data.get("data", [])
    if len(bars) < int(args.get("window", 14)) + 10:
        return _envelope({"error": "not enough bars", "n_bars": len(bars)})
    result = bt.rsi_mean_reversion(
        bars,
        window=int(args.get("window", 14)),
        lower=float(args.get("lower", 30)),
        upper=float(args.get("upper", 55)),
        fee_bps=float(args.get("fee_bps", 5)),
        symbol=args["symbol"],
    )
    return _envelope({"mode": _client.mode, **result.to_dict()})


ALL_TOOLS = [
    get_quote,
    get_candles,
    get_intraday_ticks,
    get_market_movers,
    compute_indicators,
    backtest_sma_crossover,
    backtest_rsi_mean_reversion,
]
