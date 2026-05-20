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
from . import fund_data
from . import sheets
from . import sheets_writer
from . import us_market
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


# ---------- portfolio (使用者的 Google Sheet) ----------

@tool(
    "get_my_portfolio",
    "讀取使用者目前的台股持股 — 含代號、股數、平均成本。資料來自使用者個人 "
    "Google Sheet 的「股票部位」分頁。要算現值 / 損益,需要再對每檔呼叫 get_quote。",
    {"type": "object", "properties": {}, "required": []},
)
async def get_my_portfolio(args: dict) -> dict:
    holdings = sheets.load_positions()
    if holdings and holdings[0].get("_error"):
        return _envelope({"mode": _client.mode, "error": holdings[0]["_error"]})
    return _envelope({
        "mode": _client.mode,
        "n_holdings": len(holdings),
        "holdings": holdings,
        "note": "成本價來自使用者 Google Sheet。要算現值 / 損益,請對每檔再呼叫 get_quote 取現價。",
    })


@tool(
    "get_trade_log",
    "讀取使用者完整股票交易紀錄 — 每筆 BUY / SELL 的日期、股數、成交價、手續費。"
    "資料來自使用者個人 Google Sheet 的「股票交易」分頁。用來算已實現損益、"
    "交易頻率、最長持有時間、勝率等。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "選填 — 只回傳單一代號的紀錄"},
            "action": {"type": "string", "enum": ["BUY", "SELL"], "description": "選填 — 只回傳 BUY 或 SELL"},
        },
        "required": [],
    },
)
async def get_trade_log(args: dict) -> dict:
    trades = sheets.load_trades()
    if trades and trades[0].get("_error"):
        return _envelope({"mode": _client.mode, "error": trades[0]["_error"]})
    sym = (args or {}).get("symbol")
    action = (args or {}).get("action")
    if sym:
        trades = [t for t in trades if t.get("symbol") == sym]
    if action:
        trades = [t for t in trades if t.get("action") == action.upper()]
    return _envelope({
        "mode": _client.mode,
        "n_trades": len(trades),
        "trades": trades,
    })


# ---------- 美股 / 全球(yfinance) ----------

@tool(
    "get_us_quote",
    "取得美股或全球個股 / ETF / 加密貨幣的即時報價(資料來自 Yahoo Finance,延遲 15-20 分鐘)。"
    "美股代號用英文(AAPL, MSFT, TSLA, NVDA, GOOG);ETF 同樣英文(SPY, QQQ, VOO);"
    "加密貨幣加 -USD 後綴(BTC-USD, ETH-USD)。台股代號用 get_quote,不要用這個。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Yahoo 代號,如 AAPL、SPY、BTC-USD"},
        },
        "required": ["symbol"],
    },
)
async def get_us_quote(args: dict) -> dict:
    data = us_market.quote(args["symbol"])
    return _envelope({"source": "yahoo", **data})


@tool(
    "get_us_candles",
    "取得美股或全球個股的日 K 線歷史(資料來自 Yahoo Finance)。預設過去 180 天。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "from_date": {"type": "string", "description": "ISO 日期 YYYY-MM-DD,選填"},
            "to_date": {"type": "string", "description": "ISO 日期 YYYY-MM-DD,選填"},
        },
        "required": ["symbol"],
    },
)
async def get_us_candles(args: dict) -> dict:
    data = us_market.candles(
        args["symbol"],
        from_date=args.get("from_date"),
        to_date=args.get("to_date"),
    )
    if "error" in data:
        return _envelope({"source": "yahoo", **data})
    bars = data.get("data", [])
    return _envelope({
        "source": "yahoo",
        "symbol": data.get("symbol"),
        "n_bars": len(bars),
        "first": bars[0] if bars else None,
        "last":  bars[-1] if bars else None,
        "sample_tail": bars[-10:],
    })


@tool(
    "get_stock_news",
    "取得單一個股的最新新聞(資料來自 Yahoo Finance,以英文新聞為主,有少數中文)。"
    "美股代號直接打 AAPL;台股代號要加 .TW 後綴,例如 2330 要打 2330.TW。"
    "想搜尋總體 / 政策 / 跨股票的新聞,改用 web_search 工具。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "美股代號或台股 .TW 代號"},
            "limit":  {"type": "integer", "default": 10, "minimum": 1, "maximum": 30},
        },
        "required": ["symbol"],
    },
)
async def get_stock_news(args: dict) -> dict:
    items = us_market.news(args["symbol"], limit=int(args.get("limit", 10)))
    return _envelope({"source": "yahoo", "symbol": args["symbol"],
                      "n_items": len(items), "items": items})


# ---------- 基金(Google Sheet 持有清單 + cnyes 即時 NAV) ----------

@tool(
    "get_fund_trade_log",
    "讀取使用者完整的基金買賣紀錄(「基金交易」分頁)。"
    "可依 fund_id / action 過濾。用來算已實現損益、申購頻率、平均成本變化等。",
    {
        "type": "object",
        "properties": {
            "fund_id": {"type": "string", "description": "選填 — 只回傳該基金紀錄"},
            "action":  {"type": "string", "enum": ["BUY", "SELL"]},
        },
        "required": [],
    },
)
async def get_fund_trade_log(args: dict) -> dict:
    trades = sheets.load_fund_trades()
    if trades and trades[0].get("_error"):
        return _envelope({"error": trades[0]["_error"]})
    fid = (args or {}).get("fund_id")
    action = (args or {}).get("action")
    if fid:
        trades = [t for t in trades if t.get("fund_id") == fid]
    if action:
        trades = [t for t in trades if t.get("action") == action.upper()]
    return _envelope({"n_trades": len(trades), "trades": trades})


@tool(
    "get_my_funds",
    "讀取使用者目前持有的基金(來自 Google Sheet「基金部位」分頁)。"
    "回傳每檔基金的代號、名稱、單位數、平均成本 NAV、手動填的目前 NAV(如有)、備註。"
    "要算淨值 / 損益:先用這個拿清單,再對每檔呼叫 get_fund_nav 抓即時 NAV;"
    "如果 get_fund_nav 抓不到(失敗),改用 manual_nav 兜底。",
    {"type": "object", "properties": {}, "required": []},
)
async def get_my_funds(args: dict) -> dict:
    funds = sheets.load_funds()
    if funds and funds[0].get("_error"):
        return _envelope({"error": funds[0]["_error"]})
    return _envelope({
        "n_funds": len(funds),
        "funds": funds,
        "note": "如果某檔 fund 沒有 manual_nav,先呼叫 get_fund_nav 抓即時值;"
                "如果 get_fund_nav 也失敗,就告訴使用者「需要手動更新 Sheet 上的 NAV」。",
    })


@tool(
    "get_fund_nav",
    "從鉅亨網 cnyes.com 即時抓取單一基金的最新 NAV。"
    "因為 cnyes 沒公開 API,這是 best-effort 爬蟲 — 偶爾可能抓不到,"
    "失敗時會回 {error: ...},agent 應該告訴使用者改用 Sheet 上的手動 NAV。",
    {
        "type": "object",
        "properties": {
            "fund_id": {"type": "string", "description": "鉅亨網的基金代號,如 T101.001、LU0079474960"},
        },
        "required": ["fund_id"],
    },
)
async def get_fund_nav(args: dict) -> dict:
    data = fund_data.fetch_nav(args["fund_id"])
    return _envelope({"source": "cnyes", **data})


# ---------- 寫入工具:agent 把交易記到 Sheet 並自動更新持股 ----------

@tool(
    "log_stock_trade",
    "記錄一筆台股買賣到 Google Sheet。**會同時做兩件事**:"
    "(1) 在「股票交易」分頁追加一行;(2) 在「股票部位」分頁加總或扣減該股的部位,"
    "用加權平均成本(WAC)算新 total_cost。SELL 時會回傳已實現損益。"
    "需要先部署 Apps Script Web App,並把 SHEETS_WRITER_URL 設到 Streamlit Secrets。",
    {
        "type": "object",
        "properties": {
            "date":   {"type": "string", "description": "ISO 日期 YYYY-MM-DD;若使用者沒給,用今天"},
            "symbol": {"type": "string", "description": "台股代號,如 2330、0050"},
            "action": {"type": "string", "enum": ["BUY", "SELL"]},
            "shares": {"type": "integer", "minimum": 1},
            "price":  {"type": "number",  "minimum": 0, "description": "每股成交價(NTD)"},
            "fees":   {"type": "number",  "default": 0,
                       "description": "手續費(NTD);BUY 會併入 total_cost,SELL 會從成交金額扣"},
            "tax":    {"type": "number",  "default": 0,
                       "description": "證交稅(SELL 才有,通常 = 成交金額 × 0.3%)"},
            "notes":  {"type": "string"},
        },
        "required": ["date", "symbol", "action", "shares", "price"],
    },
)
async def log_stock_trade(args: dict) -> dict:
    date    = str(args["date"]).strip()
    symbol  = str(args["symbol"]).strip()
    action  = str(args["action"]).upper().strip()
    shares  = int(args["shares"])
    price   = float(args["price"])
    fees    = float(args.get("fees") or 0)
    tax     = float(args.get("tax") or 0)
    notes   = str(args.get("notes") or "")

    # 1) 寫入「股票交易」
    log_result = sheets_writer.add_trade(
        date=date, symbol=symbol, action=action, shares=shares,
        price=price, fees=fees + tax, notes=notes,
    )
    if not log_result.get("ok"):
        return _envelope({
            "error": f"寫入股票交易失敗: {log_result.get('error')}",
            "hint":  "請確認 Apps Script Web App 已部署且 SHEETS_WRITER_URL 正確",
        })

    # 2) 讀目前股票部位
    holdings = sheets.load_positions()
    if holdings and holdings[0].get("_error"):
        return _envelope({
            "ok": False,
            "trade_logged": True,
            "position_updated": False,
            "error": f"讀取股票部位失敗: {holdings[0]['_error']}",
        })
    existing = next((h for h in holdings if h["symbol"] == symbol), None)

    # 3) 計算 + 寫回「股票部位」
    if action == "BUY":
        if existing:
            new_shares = existing["shares"] + shares
            new_total_cost = existing["total_cost"] + (shares * price) + fees
        else:
            new_shares = shares
            new_total_cost = (shares * price) + fees

        upd = sheets_writer.upsert_position(
            symbol=symbol,
            name=(existing or {}).get("name", "") or args.get("name", ""),
            shares=new_shares,
            total_cost=round(new_total_cost, 2),
            last_updated=date,
            notes=(existing or {}).get("notes", "") or "",
        )
        return _envelope({
            "ok": True,
            "action": "BUY",
            "trade_logged": True,
            "position_updated": upd.get("ok"),
            "new_shares": new_shares,
            "new_total_cost": round(new_total_cost, 2),
            "new_cost_per_share": round(new_total_cost / new_shares, 4) if new_shares else 0,
        })

    # SELL ----------
    if not existing:
        return _envelope({
            "ok": False,
            "trade_logged": True,
            "error": f"找不到 {symbol} 在股票部位中,SELL 失敗。請先在 Sheet「股票部位」分頁建一筆。",
        })
    if shares > existing["shares"]:
        return _envelope({
            "ok": False,
            "trade_logged": True,
            "error": (f"賣出股數 {shares} 大於目前持股 {existing['shares']},SELL 失敗。"
                      "請確認 Sheet 上持股數量正確。"),
        })

    cost_per_share = existing["cost_per_share"]
    cost_of_sold = shares * cost_per_share          # 加權平均扣
    proceeds = (shares * price) - fees - tax        # 淨成交金額(扣費)
    realized_pnl = proceeds - cost_of_sold
    realized_pct = (realized_pnl / cost_of_sold * 100) if cost_of_sold else 0

    new_shares = existing["shares"] - shares
    new_total_cost = max(0.0, existing["total_cost"] - cost_of_sold)

    upd = sheets_writer.upsert_position(
        symbol=symbol,
        name=existing.get("name", ""),
        shares=new_shares,
        total_cost=round(new_total_cost, 2),
        last_updated=date,
        notes=existing.get("notes", ""),
    )
    return _envelope({
        "ok": True,
        "action": "SELL",
        "trade_logged": True,
        "position_updated": upd.get("ok"),
        "remaining_shares": new_shares,
        "remaining_total_cost": round(new_total_cost, 2),
        "cost_of_sold": round(cost_of_sold, 2),
        "proceeds_net": round(proceeds, 2),
        "realized_pnl": round(realized_pnl, 2),
        "realized_pct": round(realized_pct, 2),
    })


@tool(
    "log_fund_trade",
    "記錄一筆基金的買進或贖回,**會同時做兩件事**:"
    "(1) 在「基金交易」分頁追加一行;(2) 在「基金部位」分頁加總或扣減該基金。"
    "用加權平均成本。BUY 加單位、SELL 扣單位並回傳已實現損益。",
    {
        "type": "object",
        "properties": {
            "date":    {"type": "string"},
            "fund_id": {"type": "string"},
            "action":  {"type": "string", "enum": ["BUY", "SELL"]},
            "units":   {"type": "integer", "minimum": 1},
            "nav":     {"type": "number",  "minimum": 0,
                        "description": "成交當下的單位淨值(NTD)"},
            "fees":    {"type": "number",  "default": 0},
            "notes":   {"type": "string"},
        },
        "required": ["date", "fund_id", "action", "units", "nav"],
    },
)
async def log_fund_trade(args: dict) -> dict:
    date    = str(args["date"]).strip()
    fid     = str(args["fund_id"]).strip()
    action  = str(args["action"]).upper().strip()
    units   = int(args["units"])
    nav     = float(args["nav"])
    fees    = float(args.get("fees") or 0)
    notes   = str(args.get("notes") or "")

    # 1) 先寫入「基金交易」
    log_result = sheets_writer.add_fund_trade(
        date=date, fund_id=fid, action=action,
        units=units, nav=nav, fees=fees, notes=notes,
    )
    if not log_result.get("ok"):
        return _envelope({
            "error": f"寫入基金交易失敗: {log_result.get('error')}",
            "hint":  "請確認 Apps Script Web App 已部署且 SHEETS_WRITER_URL 正確",
        })

    funds = sheets.load_funds()
    if funds and funds[0].get("_error"):
        return _envelope({"error": funds[0]["_error"], "trade_logged": True})
    existing = next((f for f in funds if f["fund_id"] == fid), None)

    if action == "BUY":
        if existing:
            new_units = existing["units"] + units
            new_total_cost = existing["total_cost"] + (units * nav) + fees
        else:
            new_units = units
            new_total_cost = (units * nav) + fees

        upd = sheets_writer.upsert_fund(
            fund_id=fid,
            name=(existing or {}).get("name", "") or args.get("name", ""),
            units=new_units,
            total_cost=round(new_total_cost, 2),
            last_updated=date,
            notes=(existing or {}).get("notes", "") or notes,
        )
        return _envelope({
            "ok": True, "action": "BUY", "fund_updated": upd.get("ok"),
            "new_units": new_units,
            "new_total_cost": round(new_total_cost, 2),
            "new_cost_per_unit": round(new_total_cost / new_units, 4) if new_units else 0,
        })

    # SELL
    if not existing:
        return _envelope({"error": f"找不到 {fid} 在基金分頁中,SELL 失敗"})
    if units > existing["units"]:
        return _envelope({"error": f"贖回單位 {units} > 持有 {existing['units']}"})

    cost_per_unit = existing["cost_per_unit"]
    cost_of_sold = units * cost_per_unit
    proceeds = (units * nav) - fees
    realized_pnl = proceeds - cost_of_sold

    new_units = existing["units"] - units
    new_total_cost = max(0.0, existing["total_cost"] - cost_of_sold)

    upd = sheets_writer.upsert_fund(
        fund_id=fid,
        name=existing.get("name", ""),
        units=new_units,
        total_cost=round(new_total_cost, 2),
        last_updated=date,
        notes=existing.get("notes", ""),
    )
    return _envelope({
        "ok": True, "action": "SELL", "fund_updated": upd.get("ok"),
        "remaining_units": new_units,
        "remaining_total_cost": round(new_total_cost, 2),
        "cost_of_sold": round(cost_of_sold, 2),
        "proceeds_net": round(proceeds, 2),
        "realized_pnl": round(realized_pnl, 2),
        "realized_pct": round((realized_pnl / cost_of_sold * 100), 2) if cost_of_sold else 0,
    })


@tool(
    "rebuild_positions_from_trades",
    "從「股票交易」分頁的歷史紀錄,**自動重建「股票部位」分頁**。"
    "依 symbol 加總 BUYs(× price + fees)減 SELLs 對應的加權平均成本,算出每檔目前的"
    "shares 與 total_cost,並覆寫到「股票部位」分頁。"
    "**適用情境**:使用者剛把歷史交易輸入完,想一鍵建立目前持股快照。"
    "已存在的同 symbol 行會被覆寫;淨股數 ≤ 0 的 symbol 會跳過。",
    {"type": "object", "properties": {}, "required": []},
)
async def rebuild_positions_from_trades(args: dict) -> dict:
    trades = sheets.load_trades()
    if trades and trades[0].get("_error"):
        return _envelope({"error": trades[0]["_error"]})

    # 依日期排序確保 SELL 順序正確
    trades_sorted = sorted(trades, key=lambda t: t.get("date", ""))

    by_sym: dict[str, dict] = {}
    for t in trades_sorted:
        sym = t.get("symbol", "")
        if not sym:
            continue
        if sym not in by_sym:
            by_sym[sym] = {"shares": 0, "total_cost": 0.0, "last_date": ""}
        row = by_sym[sym]
        action = (t.get("action") or "").upper()
        sh = int(t.get("shares") or 0)
        pr = float(t.get("price") or 0)
        fe = float(t.get("fees") or 0)
        tc = float(t.get("total_cost") or 0)

        # BUY 的成本:有填 total_cost 就直接用,沒填就 price × shares + fees
        if action == "BUY":
            cost_added = tc if tc > 0 else (sh * pr + fe)
            row["shares"] += sh
            row["total_cost"] += cost_added
        # SELL:用加權平均扣減,跟 total_cost 無關
        elif action == "SELL" and row["shares"] > 0:
            avg = row["total_cost"] / row["shares"]
            sell_shares = min(sh, row["shares"])
            row["shares"] -= sell_shares
            row["total_cost"] -= avg * sell_shares
        row["last_date"] = t.get("date", "") or row["last_date"]

    results = []
    for sym, data in by_sym.items():
        if data["shares"] <= 0:
            results.append({"symbol": sym, "skipped": "shares ≤ 0(全部賣完)"})
            continue
        upd = sheets_writer.upsert_position(
            symbol=sym,
            shares=int(data["shares"]),
            total_cost=round(data["total_cost"], 2),
            last_updated=data["last_date"],
        )
        results.append({"symbol": sym, "shares": int(data["shares"]),
                        "total_cost": round(data["total_cost"], 2),
                        "ok": upd.get("ok")})

    return _envelope({
        "ok": True,
        "rebuilt_count": sum(1 for r in results if r.get("ok")),
        "details": results,
    })


@tool(
    "rebuild_funds_from_trades",
    "從「基金交易」分頁自動重建「基金部位」分頁,概念同 rebuild_positions_from_trades,"
    "但 unit / nav / fund_id 對應。",
    {"type": "object", "properties": {}, "required": []},
)
async def rebuild_funds_from_trades(args: dict) -> dict:
    trades = sheets.load_fund_trades()
    if trades and trades[0].get("_error"):
        return _envelope({"error": trades[0]["_error"]})

    trades_sorted = sorted(trades, key=lambda t: t.get("date", ""))

    by_id: dict[str, dict] = {}
    for t in trades_sorted:
        fid = t.get("fund_id", "")
        if not fid:
            continue
        if fid not in by_id:
            by_id[fid] = {"units": 0, "total_cost": 0.0, "last_date": ""}
        row = by_id[fid]
        action = (t.get("action") or "").upper()
        u = int(t.get("units") or 0)
        nv = float(t.get("nav") or 0)
        fe = float(t.get("fees") or 0)
        tc = float(t.get("total_cost") or 0)

        if action == "BUY":
            cost_added = tc if tc > 0 else (u * nv + fe)
            row["units"] += u
            row["total_cost"] += cost_added
        elif action == "SELL" and row["units"] > 0:
            avg = row["total_cost"] / row["units"]
            sell_units = min(u, row["units"])
            row["units"] -= sell_units
            row["total_cost"] -= avg * sell_units
        row["last_date"] = t.get("date", "") or row["last_date"]

    results = []
    for fid, data in by_id.items():
        if data["units"] <= 0:
            results.append({"fund_id": fid, "skipped": "units ≤ 0"})
            continue
        upd = sheets_writer.upsert_fund(
            fund_id=fid,
            units=int(data["units"]),
            total_cost=round(data["total_cost"], 2),
            last_updated=data["last_date"],
        )
        results.append({"fund_id": fid, "units": int(data["units"]),
                        "total_cost": round(data["total_cost"], 2),
                        "ok": upd.get("ok")})

    return _envelope({
        "ok": True,
        "rebuilt_count": sum(1 for r in results if r.get("ok")),
        "details": results,
    })


@tool(
    "ping_sheets_writer",
    "測試 Apps Script Web App 是否能正常呼叫。回傳 {ok: true, pong: 時間戳} 代表通了。"
    "用來 debug SHEETS_WRITER_URL 設定。",
    {"type": "object", "properties": {}, "required": []},
)
async def ping_sheets_writer(args: dict) -> dict:
    return _envelope(sheets_writer.ping())


ALL_TOOLS = [
    get_my_portfolio,
    get_trade_log,
    get_my_funds,
    get_fund_trade_log,
    get_fund_nav,
    log_stock_trade,
    log_fund_trade,
    rebuild_positions_from_trades,
    rebuild_funds_from_trades,
    ping_sheets_writer,
    get_us_quote,
    get_us_candles,
    get_stock_news,
    get_quote,
    get_candles,
    get_intraday_ticks,
    get_market_movers,
    compute_indicators,
    backtest_sma_crossover,
    backtest_rsi_mean_reversion,
]
