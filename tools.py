"""Claude Agent SDK tool definitions.

Each tool returns the SDK-expected envelope:

    {"content": [{"type": "text", "text": <json-string>}]}

We serialize structured data as JSON inside the text block so the LLM can read
the field names directly.  Tools are intentionally read-only — this agent does
NOT place orders.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any


def _now_tw_str() -> str:
    """目前台北時間 — 給 Sheet 用的人眼可讀格式。"""
    tw = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    return tw.strftime("%Y-%m-%d %H:%M:%S")


# ---------- 中文欄位名 alias(讓 Sheet 用英中任一套 header 都行) ----------
# Apps Script 只寫到「實際存在」的 header,所以同時送英中兩個 key 沒副作用。

_ZH_ALIASES: dict[str, dict[str, str]] = {
    "positions": {
        "symbol": "代號", "name": "名稱", "shares": "股數",
        "total_cost": "總成本", "cost_per_share": "成本價",
        "last_updated": "上次更新", "notes": "備註",
        "current_price": "現價", "market_value": "市值",
        "net_proceeds": "淨賣出", "unrealized_pnl": "未實現損益",
        "pnl_pct": "損益%", "valuated_at": "估算時間",
    },
    "trades": {
        "date": "日期", "symbol": "代號", "name": "名稱",
        "action": "動作", "shares": "股數", "price": "成交價",
        "fees": "手續費", "tax": "證交稅",
        "total_cost": "總金額", "notes": "備註",
        # realized_pnl 已遷移到「實際損益」分頁,股票交易這邊就不再寫
    },
    "funds": {
        "fund_id": "代號", "name": "名稱", "units": "單位數",
        "total_cost": "總成本", "cost_per_unit": "單位成本",
        "last_updated": "上次更新", "notes": "備註",
        "current_nav": "目前NAV",
    },
    "fund_trades": {
        "date": "日期", "fund_id": "代號", "name": "名稱",
        "action": "動作", "units": "單位數", "nav": "NAV",
        "fees": "手續費", "total_cost": "總金額", "notes": "備註",
    },
    "etf_snapshot": {
        "snapshot_date": "快照日期", "etf_symbol": "ETF代號",
        "etf_name": "ETF名稱", "stock_symbol": "持股代號",
        "stock_name": "持股名稱", "weight_pct": "比例",
        "source": "資料來源", "notes": "備註",
    },
    "watchlist": {
        "symbol": "代號", "name": "名稱", "added_date": "加入日期",
        "watch_reason": "追蹤理由", "target_price": "目標價",
        "alert_when": "提醒條件", "notes": "備註",
    },
}


def _zh(payload: dict, context: str) -> dict:
    """把英文 key 的 payload 加上對應中文 alias key。
    Apps Script 只 setValue 到 sheet 上實際存在的 header,所以兩個都送沒副作用 —
    使用者用中文 header 就寫中文那欄,用英文 header 就寫英文那欄。"""
    out = dict(payload)
    for en, zh in _ZH_ALIASES.get(context, {}).items():
        if en in payload and zh not in out:
            out[zh] = payload[en]
    return out

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
from . import etf_holdings
from . import fund_data
from . import sheets
from . import sheets_writer
from . import symbol_lookup
from . import us_market
from .client import FugleClient
from .indicators import ema, rsi, sma

# Single client per process — picks mock vs live from env.
_client = FugleClient()


def _envelope(data: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}]}


# ---------- 名字自動帶入 helpers(使用者只填代號時兜底) ----------

def _lookup_stock_name(symbol: str) -> str:
    """從內建熱門表 + Fugle tickers 查股票名稱,零外部呼叫的快查。"""
    if not symbol:
        return ""
    try:
        hits = symbol_lookup.search(symbol, fugle_client=_client, limit=5)
    except Exception:
        return ""
    # 精準符合代號優先
    for h in hits:
        if str(h.get("symbol")) == str(symbol):
            return h.get("name") or ""
    # 沒精準就拿第一筆當兜底(極少觸發,因為代號是 unique key)
    return hits[0].get("name", "") if hits else ""


_FUND_NAME_CACHE: dict[str, str] = {}


def _lookup_fund_name(fund_id: str) -> str:
    """從 cnyes 抓名字,只 best-effort,失敗回空字串。
    用 in-process 快取,同個 fund_id 只打一次網路。"""
    if not fund_id:
        return ""
    if fund_id in _FUND_NAME_CACHE:
        return _FUND_NAME_CACHE[fund_id]
    try:
        data = fund_data.fetch_nav(fund_id)
        name = data.get("name") or ""
    except Exception:
        name = ""
    _FUND_NAME_CACHE[fund_id] = name
    return name


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


# ---------- 中文名稱 → 代號查詢(超重要,使用者常用名字而非代號) ----------

@tool(
    "search_taiwan_symbol",
    "依公司中文/英文名稱、暱稱、舊名,查詢正確的台股代號。"
    "**只要使用者沒明確給代號、只給名字(例如「台積電」「玉山金」「0050」「半導體 ETF」),"
    "一定要先呼叫這個工具確認代號**,不要憑記憶猜——常會猜錯到同名公司。"
    "回傳前 5 筆候選(symbol + name)。命中 1 筆就直接用,有多筆要回顯給使用者挑。",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "公司中文名、英文名、代號片段都可,例如「玉山金」「TSMC」「0050」「半導體」"},
            "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    },
)
async def search_taiwan_symbol(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    limit = int(args.get("limit") or 5)
    if not query:
        return _envelope({"error": "query 不能為空"})
    hits = symbol_lookup.search(query, fugle_client=_client, limit=limit)
    return _envelope({
        "mode":    _client.mode,
        "query":   query,
        "n_hits":  len(hits),
        "hits":    hits,
        "note":    ("命中 1 筆 → 直接使用;命中多筆 → 回顯給使用者挑;"
                    "命中 0 筆 → 告訴使用者「沒找到,請給代號」,不要硬猜。"),
    })


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
    "get_etf_holdings",
    "**抓台股 ETF 的成分股 + 產業配置**(從 MoneyDJ 理財網爬)。"
    "支援所有台股 ETF,**包含主動式 ETF(00981A、00982A 那種,cnyes / Fugle 抓不到的)**。"
    "回傳:資料日期、前 10 大持股(代號 + 名稱 + 權重% + 持有股數)、產業配置(產業 + 金額 + 比例)、source_url。"
    "使用者問「00981A 持股是什麼」「0050 成分股」「00878 配置」「半導體 ETF 拿了哪些股」→ 用這個。"
    "前 10 大以外的完整持股 MoneyDJ 主頁不顯示,要看請點 source_url。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string",
                       "description": "台股 ETF 代號,如 0050、00878、00981A、006208"},
        },
        "required": ["symbol"],
    },
)
async def get_etf_holdings(args: dict) -> dict:
    sym = str(args.get("symbol") or "").strip()
    if not sym:
        return _envelope({"error": "缺少 symbol"})
    data = etf_holdings.fetch_holdings(sym)
    return _envelope(data)


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
    "**name 不用填**,系統會自己用 symbol_lookup 查名字補上。",
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
            "name":   {"type": "string",
                       "description": "選填,使用者沒給時系統自己從內建表查"},
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
    name_in = str(args.get("name") or "").strip()  # 使用者顯式給的名字(罕見)

    # 1) 寫入「股票交易」— fees 跟 tax 分開寫,方便對帳
    auto_name = name_in or _lookup_stock_name(symbol)
    log_result = sheets_writer.add_trade(**_zh({
        "date": date, "symbol": symbol, "name": auto_name,
        "action": action, "shares": shares,
        "price": price,
        "fees": fees,    # 純手續費
        "tax":  tax,     # 證交稅(BUY 通常為 0)
        "notes": notes,
    }, "trades"))
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

        upd = sheets_writer.upsert_position(**_zh({
            "symbol": symbol,
            "name": (existing or {}).get("name") or auto_name,
            "shares": new_shares,
            "total_cost": round(new_total_cost, 2),
            "last_updated": date,
            "notes": (existing or {}).get("notes", "") or "",
        }, "positions"))

        # 觸發 Apps Script 全套同步(目標賣價公式、realized_pnl 等)
        sheets_writer.manual_sync()

        # 順便跑一次估值,把現價 / 市值 / 損益填進股票部位
        try:
            await valuate_portfolio.handler({"write_back": True})
        except Exception:
            pass  # 估值失敗不應該影響交易紀錄寫入

        return _envelope({
            "ok": True,
            "action": "BUY",
            "trade_logged": True,
            "position_updated": upd.get("ok"),
            "name_used": (existing or {}).get("name") or auto_name,
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

    upd = sheets_writer.upsert_position(**_zh({
        "symbol": symbol,
        "name": existing.get("name") or auto_name,
        "shares": new_shares,
        "total_cost": round(new_total_cost, 2),
        "last_updated": date,
        "notes": existing.get("notes", ""),
    }, "positions"))

    # 把這筆 SELL 的 realized_pnl 寫回「股票交易」剛剛新增的那一列
    realized_writeback = sheets_writer.update_trade_realized(
        date=date, symbol=symbol, action="SELL", shares=shares,
        realized_pnl=round(realized_pnl, 2),
    )

    # 觸發 Apps Script 全套同步(實際損益 tab、目標賣價公式…一起到位)
    sheets_writer.manual_sync()

    # 順便跑一次估值,讓股票部位的現價/市值/損益欄都新鮮
    try:
        await valuate_portfolio.handler({"write_back": True})
    except Exception:
        pass

    return _envelope({
        "ok": True,
        "action": "SELL",
        "realized_pnl_written_to_trade_row": bool(realized_writeback.get("ok")),
        "realized_pnl_write_error": realized_writeback.get("error"),
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
    "用加權平均成本。BUY 加單位、SELL 扣單位並回傳已實現損益。"
    "**name 不用填**,系統會自己從 cnyes 查名字補上(best-effort)。",
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
            "name":    {"type": "string",  "description": "選填,系統會自己抓"},
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
    name_in = str(args.get("name") or "").strip()

    # 自動補基金名(cnyes 抓,失敗就空字串)
    auto_name = name_in or _lookup_fund_name(fid)

    # 1) 先寫入「基金交易」— 連 name 一起帶
    log_result = sheets_writer.add_fund_trade(**_zh({
        "date": date, "fund_id": fid, "name": auto_name, "action": action,
        "units": units, "nav": nav, "fees": fees, "notes": notes,
    }, "fund_trades"))
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

        upd = sheets_writer.upsert_fund(**_zh({
            "fund_id": fid,
            "name": (existing or {}).get("name") or auto_name,
            "units": new_units,
            "total_cost": round(new_total_cost, 2),
            "last_updated": date,
            "notes": (existing or {}).get("notes", "") or notes,
        }, "funds"))
        return _envelope({
            "ok": True, "action": "BUY", "fund_updated": upd.get("ok"),
            "name_used": (existing or {}).get("name") or auto_name,
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

    upd = sheets_writer.upsert_fund(**_zh({
        "fund_id": fid,
        "name": existing.get("name") or auto_name,
        "units": new_units,
        "total_cost": round(new_total_cost, 2),
        "last_updated": date,
        "notes": existing.get("notes", ""),
    }, "funds"))
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

    # 預讀目前股票部位,拿到既有 name(若有)當第一手
    existing_names: dict[str, str] = {}
    cur_positions = sheets.load_positions()
    if cur_positions and not (cur_positions[0].get("_error")):
        existing_names = {p["symbol"]: p.get("name", "") for p in cur_positions}

    results = []
    for sym, data in by_sym.items():
        if data["shares"] <= 0:
            results.append({"symbol": sym, "skipped": "shares ≤ 0(全部賣完)"})
            continue
        # name 優先序:既有 sheet 上有的 → symbol_lookup 查 → 空字串
        nm = existing_names.get(sym) or _lookup_stock_name(sym)
        upd = sheets_writer.upsert_position(
            symbol=sym,
            name=nm,
            shares=int(data["shares"]),
            total_cost=round(data["total_cost"], 2),
            last_updated=data["last_date"],
        )
        results.append({"symbol": sym, "name": nm, "shares": int(data["shares"]),
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

    # 預讀既有基金部位拿 name
    existing_fnames: dict[str, str] = {}
    cur_funds = sheets.load_funds()
    if cur_funds and not (cur_funds[0].get("_error")):
        existing_fnames = {f["fund_id"]: f.get("name", "") for f in cur_funds}

    results = []
    for fid, data in by_id.items():
        if data["units"] <= 0:
            results.append({"fund_id": fid, "skipped": "units ≤ 0"})
            continue
        # name 優先:既有 → cnyes 查(慢)→ 空
        nm = existing_fnames.get(fid) or _lookup_fund_name(fid)
        upd = sheets_writer.upsert_fund(
            fund_id=fid,
            name=nm,
            units=int(data["units"]),
            total_cost=round(data["total_cost"], 2),
            last_updated=data["last_date"],
        )
        results.append({"fund_id": fid, "name": nm, "units": int(data["units"]),
                        "total_cost": round(data["total_cost"], 2),
                        "ok": upd.get("ok")})

    return _envelope({
        "ok": True,
        "rebuilt_count": sum(1 for r in results if r.get("ok")),
        "details": results,
    })


@tool(
    "backfill_position_names",
    "**一次性**幫「股票部位」分頁裡空白的 name 欄補名字。"
    "讀現有部位 → 對 name 是空的 row 用內建表查名字 → 只更新 name 欄(其他欄位不動)。"
    "查不到的會跳過(不會亂寫名字)。回傳每檔的處理結果。"
    "適用情境:使用者過去填部位時沒填名稱,想一鍵回補。",
    {
        "type": "object",
        "properties": {
            "force": {"type": "boolean", "default": False,
                      "description": "True = 連已有 name 的也用內建表覆寫(很少需要)"},
        },
        "required": [],
    },
)
async def backfill_position_names(args: dict) -> dict:
    force = bool((args or {}).get("force"))
    positions = sheets.load_positions()
    if positions and positions[0].get("_error"):
        return _envelope({"error": positions[0]["_error"]})

    results = []
    filled = 0
    for p in positions:
        sym = p.get("symbol", "")
        cur_name = (p.get("name") or "").strip()
        if cur_name and not force:
            results.append({"symbol": sym, "name": cur_name, "skipped": "已有 name"})
            continue
        new_name = _lookup_stock_name(sym)
        if not new_name:
            results.append({"symbol": sym, "skipped": "內建表查不到"})
            continue
        upd = sheets_writer.upsert_position(symbol=sym, name=new_name)
        if upd.get("ok"):
            filled += 1
            results.append({"symbol": sym, "name_filled": new_name})
        else:
            results.append({"symbol": sym, "error": upd.get("error")})

    return _envelope({
        "ok": True,
        "filled_count": filled,
        "total_positions": len(positions),
        "details": results,
    })


@tool(
    "backfill_fund_names",
    "**一次性**幫「基金部位」分頁裡空白的 name 欄補名字(從 cnyes 鉅亨網抓)。"
    "best-effort — cnyes 可能抓不到的就跳過。其他欄位不動。",
    {
        "type": "object",
        "properties": {
            "force": {"type": "boolean", "default": False},
        },
        "required": [],
    },
)
async def backfill_fund_names(args: dict) -> dict:
    force = bool((args or {}).get("force"))
    funds = sheets.load_funds()
    if funds and funds[0].get("_error"):
        return _envelope({"error": funds[0]["_error"]})

    results = []
    filled = 0
    for f in funds:
        fid = f.get("fund_id", "")
        cur_name = (f.get("name") or "").strip()
        if cur_name and not force:
            results.append({"fund_id": fid, "name": cur_name, "skipped": "已有 name"})
            continue
        new_name = _lookup_fund_name(fid)
        if not new_name:
            results.append({"fund_id": fid, "skipped": "cnyes 抓不到名字"})
            continue
        upd = sheets_writer.upsert_fund(fund_id=fid, name=new_name)
        if upd.get("ok"):
            filled += 1
            results.append({"fund_id": fid, "name_filled": new_name})
        else:
            results.append({"fund_id": fid, "error": upd.get("error")})

    return _envelope({
        "ok": True,
        "filled_count": filled,
        "total_funds": len(funds),
        "details": results,
    })


@tool(
    "valuate_portfolio",
    "**估算現在如果全部賣掉的真實淨損益**(已扣手續費 + 證交稅)。"
    "對每檔股票部位:"
    "(1) 抓即時報價(Fugle live;失敗用 Sheet 上 current_price 兜底);"
    "(2) ETF(代號 00 開頭如 0050、00878)套證交稅 0.1%、一般股套 0.3%;"
    "(3) 套使用者手續費率(預設 0.1425%、下限 NT$1);"
    "(4) 算淨賣出金額、未實現損益、損益%、加總。"
    "**(5) 預設會把結果寫回「股票部位」分頁的 market_value / net_proceeds / "
    "unrealized_pnl / pnl_pct / valuated_at 欄位**(沒有這些欄位就會自動忽略,"
    "不會打壞 Sheet)。傳 write_back=false 可以只算不寫。"
    "回傳:每檔一行明細 + 總計。",
    {
        "type": "object",
        "properties": {
            "fee_rate": {"type": "number",
                         "description": "選填,override 預設 0.1425%(用小數,如 0.001425)"},
            "fee_min":  {"type": "number",
                         "description": "選填,override 預設 NT$1 手續費下限"},
            "write_back": {"type": "boolean", "default": True,
                           "description": "True(預設)= 算完寫回 Sheet;False = 只算不寫"},
        },
        "required": [],
    },
)
async def valuate_portfolio(args: dict) -> dict:
    fee_rate = float((args or {}).get("fee_rate")
                     or os.getenv("USER_FEE_RATE", "0.001425"))
    fee_min  = float((args or {}).get("fee_min")
                     or os.getenv("USER_FEE_MIN", "1"))
    write_back = bool((args or {}).get("write_back", True))
    valuated_at = _now_tw_str()

    positions = sheets.load_positions()
    if positions and positions[0].get("_error"):
        return _envelope({"error": positions[0]["_error"]})

    # 預先讀使用者實際的 sheet headers,看「市值 / 估算時間…」這些欄位到底有沒有加
    valuation_headers_en = {"current_price", "market_value", "net_proceeds",
                            "unrealized_pnl", "pnl_pct", "valuated_at"}
    valuation_headers_zh = {"現價", "市值", "淨賣出", "未實現損益", "損益%", "估算時間"}
    sheet_headers: set[str] = set()
    if write_back:
        tab_name = os.getenv(sheets.POSITIONS_TAB_ENV, sheets.DEFAULT_POSITIONS_TAB)
        raw = sheets.fetch_tab(tab_name)
        if raw and not raw[0].get("_error"):
            sheet_headers = set(raw[0].keys())
    matched_val_headers = (valuation_headers_en | valuation_headers_zh) & sheet_headers

    rows: list[dict] = []
    sum_cost = sum_gross = sum_fee = sum_tax = sum_net = 0.0

    for p in positions:
        sym    = str(p.get("symbol", "")).strip()
        shares = int(p.get("shares") or 0)
        total_cost = float(p.get("total_cost") or 0)
        if shares <= 0 or total_cost <= 0:
            continue

        # 1) 抓即時價 — Fugle 優先,失敗用 Sheet 上的 manual current_price
        price: float | None = None
        price_source = "none"
        try:
            q = _client.quote(sym)
            for k in ("lastPrice", "closePrice", "price",
                      "referencePrice", "previousClose"):
                v = q.get(k) if isinstance(q, dict) else None
                if v:
                    price = float(v)
                    price_source = f"fugle.{k}"
                    break
        except Exception:
            pass
        if price is None or price <= 0:
            manual = p.get("current_price")
            if manual:
                try:
                    price = float(manual)
                    price_source = "sheet.current_price"
                except (TypeError, ValueError):
                    price = None

        if not price:
            rows.append({
                "symbol":     sym,
                "name":       p.get("name", ""),
                "shares":     shares,
                "total_cost": round(total_cost, 2),
                "error":      "現價抓不到 — Fugle 失敗且 Sheet 沒填 current_price",
            })
            continue

        # 2) 算稅率(ETF vs 一般股)
        is_etf = sym.startswith("00") and len(sym) >= 4
        tax_rate = 0.001 if is_etf else 0.003

        # 3) 套公式
        gross = price * shares
        fee   = max(fee_min, gross * fee_rate)
        tax   = gross * tax_rate
        net   = gross - fee - tax
        pnl   = net - total_cost
        pct   = (pnl / total_cost * 100) if total_cost else 0.0

        sum_cost  += total_cost
        sum_gross += gross
        sum_fee   += fee
        sum_tax   += tax
        sum_net   += net

        row = {
            "symbol":         sym,
            "name":           p.get("name", ""),
            "shares":         shares,
            "cost_per_share": round(total_cost / shares, 4) if shares else 0,
            "total_cost":     round(total_cost, 2),
            "current_price":  round(price, 4),
            "price_source":   price_source,
            "is_etf":         is_etf,
            "tax_rate":       tax_rate,
            "gross_proceeds": round(gross, 2),
            "sell_fee":       round(fee, 2),
            "sell_tax":       round(tax, 2),
            "net_proceeds":   round(net, 2),
            "unrealized_pnl": round(pnl, 2),
            "pnl_pct":        round(pct, 2),
            "valuated_at":    valuated_at,
        }

        # 寫回 Sheet — 連英文 + 中文 header 一起送,只有 sheet 上實際存在的欄位會被填
        if write_back:
            sheet_payload = {
                "symbol":         sym,
                "current_price":  round(price, 4),
                "market_value":   round(gross, 2),
                "net_proceeds":   round(net, 2),
                "unrealized_pnl": round(pnl, 2),
                "pnl_pct":        round(pct, 2),
                "valuated_at":    valuated_at,
                # 中文 alias(讓使用者愛用哪套都行)
                "現價":           round(price, 4),
                "市值":           round(gross, 2),
                "淨賣出":         round(net, 2),
                "未實現損益":     round(pnl, 2),
                "損益%":          round(pct, 2),
                "估算時間":       valuated_at,
            }
            wb = sheets_writer.upsert_position(**sheet_payload)
            row["written_to_sheet"] = bool(wb.get("ok"))

        rows.append(row)

    total_pnl     = sum_net - sum_cost
    total_pnl_pct = (total_pnl / sum_cost * 100) if sum_cost else 0.0

    # 診斷:write_back=True 但 sheet 上 0 個目標欄位 → 寫出去也不會被填,要主動警告
    write_warning = None
    if write_back:
        if not sheet_headers:
            write_warning = ("讀不到「股票部位」的欄位列;可能 PORTFOLIO_SHEET_URL 沒設或"
                             "分頁名稱對不上,寫回會被 Apps Script 拒絕。")
        elif not matched_val_headers:
            write_warning = (
                "你的「股票部位」分頁**還沒加任何估值欄位**,所以剛才送的數字"
                "全部被 Apps Script 忽略(不會壞、但也不會填)。"
                "請到 Sheet 加至少一欄,英文版任選: "
                "current_price / market_value / net_proceeds / unrealized_pnl / pnl_pct / valuated_at;"
                "或中文版任選: 現價 / 市值 / 淨賣出 / 未實現損益 / 損益% / 估算時間。"
            )

    return _envelope({
        "ok":            True,
        "mode":          _client.mode,
        "valuated_at":   valuated_at,
        "write_back":    write_back,
        "fee_rate_used": fee_rate,
        "fee_min_used":  fee_min,
        "n_positions":   len(rows),
        "positions":     rows,
        "summary": {
            "total_cost":           round(sum_cost, 2),
            "total_gross_proceeds": round(sum_gross, 2),
            "total_sell_fee":       round(sum_fee, 2),
            "total_sell_tax":       round(sum_tax, 2),
            "total_net_proceeds":   round(sum_net, 2),
            "total_unrealized_pnl": round(total_pnl, 2),
            "total_pnl_pct":        round(total_pnl_pct, 2),
        },
        "sheet_diagnostics": {
            "sheet_headers_found":   sorted(sheet_headers),
            "matched_val_headers":   sorted(matched_val_headers),
            "missing_val_headers":   sorted((valuation_headers_en | valuation_headers_zh) - sheet_headers),
            "warning":               write_warning,
        },
        "note": ("未實現損益 = 假設現在全部賣掉、扣完手續費 + 證交稅之後的淨收入 - 你的總成本。"
                 "結果已嘗試寫回 Sheet「股票部位」分頁(請看 sheet_diagnostics 確認哪些欄位匹配到了)。"
                 if write_back else
                 "未實現損益 = ...(略)。本次未寫回 Sheet(write_back=false)。"),
    })


@tool(
    "sync_portfolio_from_trades",
    "**一鍵全套同步**:從「股票交易」歷史 → 重建「股票部位」每檔的 shares + total_cost → "
    "對每檔自動補名字 → 抓 Fugle 即時價 → 套手續費 + 稅算出市值/淨賣出/未實現損益 → "
    "**全部寫回 Sheet**(包含部位欄位 + 估值欄位 + 估算時間)。"
    "適用情境:使用者剛**手動加了交易**,想要一次把所有東西算好填滿,不用分兩步呼叫。"
    "Sheet 上沒加的估值欄位會被忽略(不會壞),回應裡有 sheet_diagnostics 告訴你哪些匹配到。",
    {
        "type": "object",
        "properties": {
            "fee_rate": {"type": "number"},
            "fee_min":  {"type": "number"},
        },
        "required": [],
    },
)
async def sync_portfolio_from_trades(args: dict) -> dict:
    fee_rate = float((args or {}).get("fee_rate")
                     or os.getenv("USER_FEE_RATE", "0.001425"))
    fee_min  = float((args or {}).get("fee_min")
                     or os.getenv("USER_FEE_MIN", "1"))
    valuated_at = _now_tw_str()

    # ─── 1) 讀股票交易 ─────────────────────────────────────────
    trades = sheets.load_trades()
    if trades and trades[0].get("_error"):
        return _envelope({"error": trades[0]["_error"]})

    # ─── 2) 依日期排序,在記憶體裡跑加總 / 加權平均扣減 ──────
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
        if action == "BUY":
            cost_added = tc if tc > 0 else (sh * pr + fe)
            row["shares"] += sh
            row["total_cost"] += cost_added
        elif action == "SELL" and row["shares"] > 0:
            avg = row["total_cost"] / row["shares"]
            sell_shares = min(sh, row["shares"])
            row["shares"] -= sell_shares
            row["total_cost"] -= avg * sell_shares
        row["last_date"] = t.get("date", "") or row["last_date"]

    # ─── 3) 讀目前部位,拿 name / notes / current_price 兜底 ─────
    cur_positions = sheets.load_positions()
    cur_meta: dict[str, dict] = {}
    if cur_positions and not (cur_positions[0].get("_error")):
        cur_meta = {p["symbol"]: p for p in cur_positions}

    # 探 sheet 實際存在哪些估值欄位,讓回應能告訴使用者匹配狀況
    valuation_headers_en = {"current_price", "market_value", "net_proceeds",
                            "unrealized_pnl", "pnl_pct", "valuated_at"}
    valuation_headers_zh = {"現價", "市值", "淨賣出", "未實現損益", "損益%", "估算時間"}
    tab_name = os.getenv(sheets.POSITIONS_TAB_ENV, sheets.DEFAULT_POSITIONS_TAB)
    raw = sheets.fetch_tab(tab_name)
    sheet_headers: set[str] = (set(raw[0].keys()) if (raw and not raw[0].get("_error"))
                               else set())
    matched_val_headers = (valuation_headers_en | valuation_headers_zh) & sheet_headers

    # ─── 4) 對每檔抓即時價 + 算估值 + 寫回 ─────────────────────
    details: list[dict] = []
    sum_cost = sum_gross = sum_fee = sum_tax = sum_net = 0.0

    for sym, data in by_sym.items():
        shares = int(data["shares"])
        total_cost = round(data["total_cost"], 2)
        if shares <= 0 or total_cost <= 0:
            details.append({"symbol": sym, "skipped": "shares ≤ 0(全部賣完)"})
            continue

        meta = cur_meta.get(sym, {})
        name  = meta.get("name") or _lookup_stock_name(sym)
        notes = meta.get("notes", "")

        # 抓即時價:Fugle 優先,失敗用 Sheet 上原本的 current_price
        price: float | None = None
        price_source = "none"
        try:
            q = _client.quote(sym)
            for k in ("lastPrice", "closePrice", "price",
                      "referencePrice", "previousClose"):
                v = q.get(k) if isinstance(q, dict) else None
                if v:
                    price = float(v)
                    price_source = f"fugle.{k}"
                    break
        except Exception:
            pass
        if not price:
            manual = meta.get("current_price")
            if manual:
                try:
                    price = float(manual)
                    price_source = "sheet.current_price"
                except (TypeError, ValueError):
                    price = None

        is_etf   = sym.startswith("00") and len(sym) >= 4
        tax_rate = 0.001 if is_etf else 0.003

        # 部位基本欄位(這些一定寫)
        payload: dict[str, Any] = {
            "symbol":       sym,
            "name":         name,
            "shares":       shares,
            "total_cost":   total_cost,
            "last_updated": data["last_date"],
        }
        if notes:
            payload["notes"] = notes

        # 估值欄位(有抓到價才算)
        if price and price > 0:
            gross = price * shares
            fee   = max(fee_min, gross * fee_rate)
            tax   = gross * tax_rate
            net   = gross - fee - tax
            pnl   = net - total_cost
            pct   = (pnl / total_cost * 100) if total_cost else 0.0

            sum_cost  += total_cost
            sum_gross += gross
            sum_fee   += fee
            sum_tax   += tax
            sum_net   += net

            payload.update({
                "current_price":  round(price, 4),
                "market_value":   round(gross, 2),
                "net_proceeds":   round(net, 2),
                "unrealized_pnl": round(pnl, 2),
                "pnl_pct":        round(pct, 2),
                "valuated_at":    valuated_at,
                # 中文 alias
                "現價":           round(price, 4),
                "市值":           round(gross, 2),
                "淨賣出":         round(net, 2),
                "未實現損益":     round(pnl, 2),
                "損益%":          round(pct, 2),
                "估算時間":       valuated_at,
            })
            row_summary = {
                "symbol": sym, "name": name, "shares": shares,
                "total_cost": total_cost,
                "current_price": round(price, 4),
                "price_source": price_source,
                "unrealized_pnl": round(pnl, 2),
                "pnl_pct": round(pct, 2),
            }
        else:
            row_summary = {
                "symbol": sym, "name": name, "shares": shares,
                "total_cost": total_cost,
                "warning": "抓不到現價,估值欄位這次不寫;部位本身已更新",
            }

        upd = sheets_writer.upsert_position(**payload)
        row_summary["written_to_sheet"] = bool(upd.get("ok"))
        if not upd.get("ok"):
            row_summary["error"] = upd.get("error")
        details.append(row_summary)

    total_pnl     = sum_net - sum_cost
    total_pnl_pct = (total_pnl / sum_cost * 100) if sum_cost else 0.0

    write_warning = None
    if not sheet_headers:
        write_warning = ("讀不到「股票部位」分頁的欄位 — 確認 PORTFOLIO_SHEET_URL 設好。")
    elif not matched_val_headers:
        write_warning = (
            "你的「股票部位」分頁還沒加估值欄位,所以市值/損益等數字寫不進去。"
            "請加任一英文或中文欄位:current_price / market_value / net_proceeds / "
            "unrealized_pnl / pnl_pct / valuated_at(或對應中文 現價 / 市值 / "
            "淨賣出 / 未實現損益 / 損益% / 估算時間)。"
        )

    return _envelope({
        "ok":            True,
        "mode":          _client.mode,
        "valuated_at":   valuated_at,
        "fee_rate_used": fee_rate,
        "fee_min_used":  fee_min,
        "n_positions":   sum(1 for d in details if not d.get("skipped")),
        "details":       details,
        "summary": {
            "total_cost":           round(sum_cost, 2),
            "total_gross_proceeds": round(sum_gross, 2),
            "total_sell_fee":       round(sum_fee, 2),
            "total_sell_tax":       round(sum_tax, 2),
            "total_net_proceeds":   round(sum_net, 2),
            "total_unrealized_pnl": round(total_pnl, 2),
            "total_pnl_pct":        round(total_pnl_pct, 2),
        },
        "sheet_diagnostics": {
            "sheet_headers_found": sorted(sheet_headers),
            "matched_val_headers": sorted(matched_val_headers),
            "warning":             write_warning,
        },
        "note": ("一鍵同步完成:重建部位 + 抓即時價 + 算估值 + 寫回 Sheet。"
                 "下次再加交易,直接再叫一次就好。"),
    })


@tool(
    "get_realized_pnl",
    "**掃整份「股票交易」紀錄,算出每筆 SELL 的已實現損益(實際賺/賠多少錢)**。"
    "用加權平均成本法(跟玉山 e Trader 庫存顯示的「平均成本」一致):"
    "依日期排序,BUY 加股數+成本,SELL 用當下的平均成本扣減。"
    "回傳:總已實現損益、勝率、每檔加總、每筆 SELL 的細節。"
    "**支援 write_back=true 把每筆 SELL 的 realized_pnl 寫回「股票交易」對應 row**"
    "(需要「股票交易」分頁有 realized_pnl 或 已實現損益 欄位)。"
    "適用情境:使用者問「我交易賺多少」「已實現損益」「過去賣掉賺了多少」「我的勝率」「幫我把每筆 SELL 的賺賠記到 Sheet」。",
    {
        "type": "object",
        "properties": {
            "from_date":  {"type": "string", "description": "選填,只算這個日期之後的 SELL"},
            "to_date":    {"type": "string", "description": "選填,只算這個日期之前的 SELL"},
            "symbol":     {"type": "string", "description": "選填,只算單一代號"},
            "write_back": {"type": "boolean", "default": False,
                           "description": "True = 把每筆 SELL 的 realized_pnl 寫回「股票交易」對應 row"},
        },
        "required": [],
    },
)
async def get_realized_pnl(args: dict) -> dict:
    from_date  = str((args or {}).get("from_date") or "").strip()
    to_date    = str((args or {}).get("to_date")   or "").strip()
    sym_filter = str((args or {}).get("symbol")    or "").strip()
    write_back = bool((args or {}).get("write_back", False))

    trades = sheets.load_trades()
    if trades and trades[0].get("_error"):
        return _envelope({"error": trades[0]["_error"]})

    # 依日期排序(SELL 才能正確 reference 當下的加權平均成本)
    trades_sorted = sorted(trades, key=lambda t: str(t.get("date") or ""))

    by_sym: dict[str, dict] = {}     # 跑加權平均的中間狀態
    events: list[dict] = []          # 每筆 SELL 的紀錄

    for t in trades_sorted:
        sym = str(t.get("symbol", "")).strip()
        if not sym:
            continue
        date_str = str(t.get("date") or "")
        action = (t.get("action") or "").upper().strip()
        sh = int(t.get("shares") or 0)
        pr = float(t.get("price") or 0)
        fe = float(t.get("fees")  or 0)  # 注意:這欄是 手續費 + 證交稅 加總
        tc = float(t.get("total_cost") or 0)

        if sym not in by_sym:
            by_sym[sym] = {"shares": 0, "total_cost": 0.0}
        st = by_sym[sym]

        if action == "BUY":
            cost_added = tc if tc > 0 else (sh * pr + fe)
            st["shares"]     += sh
            st["total_cost"] += cost_added
        elif action == "SELL" and st["shares"] > 0:
            avg = st["total_cost"] / st["shares"] if st["shares"] else 0
            sell_shares  = min(sh, st["shares"])
            cost_of_sold = avg * sell_shares
            # 淨收入:有填 total_cost 就直接用,沒填就 price×shares - fees(已含稅)
            proceeds = tc if tc > 0 else (sh * pr - fe)
            realized = proceeds - cost_of_sold
            pct      = (realized / cost_of_sold * 100) if cost_of_sold else 0.0

            # 過濾(日期區間 + 代號)
            ok_date = ((not from_date or date_str >= from_date)
                       and (not to_date or date_str <= to_date))
            ok_sym  = (not sym_filter or sym == sym_filter)
            if ok_date and ok_sym:
                events.append({
                    "date":             date_str,
                    "symbol":           sym,
                    "name":             _lookup_stock_name(sym),
                    "shares_sold":      sell_shares,
                    "sell_price":       round(pr, 4),
                    "avg_cost_at_sale": round(avg, 4),
                    "cost_of_sold":     round(cost_of_sold, 2),
                    "fees_and_tax":     round(fe, 2),
                    "proceeds_net":     round(proceeds, 2),
                    "realized_pnl":     round(realized, 2),
                    "realized_pct":     round(pct, 2),
                })

            # 不管在不在過濾範圍內,都要更新狀態(SELL 的扣減是累積的)
            st["shares"]     -= sell_shares
            st["total_cost"] -= cost_of_sold

    # 加總
    n_sells       = len(events)
    total_real    = sum(e["realized_pnl"] for e in events)
    wins          = sum(1 for e in events if e["realized_pnl"] > 0)
    losses        = sum(1 for e in events if e["realized_pnl"] < 0)
    flat          = n_sells - wins - losses
    win_rate      = (wins / n_sells * 100) if n_sells else 0.0

    # 每檔加總
    by_sym_real: dict[str, dict] = {}
    for e in events:
        s = e["symbol"]
        if s not in by_sym_real:
            by_sym_real[s] = {"symbol": s, "name": e["name"],
                              "total_realized": 0.0, "n_sells": 0}
        by_sym_real[s]["total_realized"] += e["realized_pnl"]
        by_sym_real[s]["n_sells"]        += 1
    by_sym_list = sorted(
        ({"symbol": v["symbol"], "name": v["name"],
          "total_realized": round(v["total_realized"], 2),
          "n_sells": v["n_sells"]}
         for v in by_sym_real.values()),
        key=lambda x: -x["total_realized"],
    )

    # 寫回 — 對每筆 SELL 嘗試把 realized_pnl 填到「股票交易」對應 row
    write_results: list[dict] = []
    if write_back and events:
        for e in events:
            res = sheets_writer.update_trade_realized(
                date=e["date"], symbol=e["symbol"], action="SELL",
                shares=e["shares_sold"],
                realized_pnl=e["realized_pnl"],
            )
            write_results.append({
                "date": e["date"], "symbol": e["symbol"], "shares": e["shares_sold"],
                "realized_pnl": e["realized_pnl"],
                "ok": bool(res.get("ok")),
                "error": res.get("error"),
            })

    write_summary = None
    if write_back:
        wins_write = sum(1 for r in write_results if r["ok"])
        write_summary = {
            "attempted":  len(write_results),
            "succeeded":  wins_write,
            "failed":     len(write_results) - wins_write,
            "details":    write_results,
            "hint": ("失敗最常見原因:(1)「股票交易」分頁沒加 realized_pnl 或 已實現損益 欄位 "
                     "(2) Apps Script 還沒重新部署最新版 "
                     "(3) date / symbol / shares 跟 Sheet 上的不完全一致。"),
        }

    return _envelope({
        "ok":                 True,
        "filter_from":        from_date or None,
        "filter_to":          to_date or None,
        "filter_symbol":      sym_filter or None,
        "n_sells":            n_sells,
        "total_realized_pnl": round(total_real, 2),
        "win_count":          wins,
        "loss_count":         losses,
        "flat_count":         flat,
        "win_rate_pct":       round(win_rate, 2),
        "by_symbol":          by_sym_list,
        "events":             events,
        "write_back":         write_summary,
        "note": ("已實現損益 = 賣出淨收 - 當下加權平均成本 × 賣出股數。"
                 "費用欄已含手續費 + 證交稅。整體勝率 = 賺錢的 SELL 筆數 / 總 SELL 筆數。"),
    })


@tool(
    "record_etf_snapshot",
    "**把 ETF 持股快照寫進 Sheet「ETF快照」分頁** — 一檔股票一個 row,"
    "用來累積使用者自己的 ETF 持股時序資料庫(主動式 ETF 經理人調倉趨勢)。"
    "適用情境:使用者上傳官方/聚合商的持股截圖,你解析後**先回顯給使用者確認,確認後**才呼叫這個。"
    "使用者只需給日期、ETF 代號、持股清單(代號 + 權重);名字會自動補。",
    {
        "type": "object",
        "properties": {
            "snapshot_date": {"type": "string", "description": "資料日期 YYYY-MM-DD"},
            "etf_symbol":    {"type": "string", "description": "ETF 代號"},
            "etf_name":      {"type": "string", "description": "選填,沒給會用 search_taiwan_symbol 查"},
            "source":        {"type": "string", "description": "資料來源,如「統一投信官網」「MoneyDJ 截圖」"},
            "holdings": {
                "type": "array",
                "description": "持股清單,可一次傳多筆(通常前 10 大)",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol":     {"type": "string"},
                        "name":       {"type": "string", "description": "選填"},
                        "weight_pct": {"type": "number", "description": "權重 %"},
                    },
                    "required": ["symbol", "weight_pct"],
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["snapshot_date", "etf_symbol", "holdings"],
    },
)
async def record_etf_snapshot(args: dict) -> dict:
    date     = str(args["snapshot_date"]).strip()
    etf_sym  = str(args["etf_symbol"]).strip().upper()
    etf_name = str(args.get("etf_name") or "").strip() or _lookup_stock_name(etf_sym)
    source   = str(args.get("source") or "").strip()
    notes    = str(args.get("notes") or "").strip()
    holdings = args.get("holdings") or []
    if not holdings:
        return _envelope({"error": "持股清單是空的"})

    results: list[dict] = []
    n_ok = 0
    for h in holdings:
        sym = str(h.get("symbol", "")).strip()
        if not sym:
            results.append({"symbol": "", "error": "缺少 symbol"})
            continue
        name = str(h.get("name") or "").strip() or _lookup_stock_name(sym)
        try:
            weight = float(h.get("weight_pct") or 0)
        except (TypeError, ValueError):
            weight = 0.0
        res = sheets_writer.add_etf_snapshot(**_zh({
            "snapshot_date": date,
            "etf_symbol":    etf_sym,
            "etf_name":      etf_name,
            "stock_symbol":  sym,
            "stock_name":    name,
            "weight_pct":    round(weight, 4),
            "source":        source,
            "notes":         notes,
        }, "etf_snapshot"))
        ok = bool(res.get("ok"))
        if ok:
            n_ok += 1
        results.append({
            "symbol":     sym,
            "name":       name,
            "weight_pct": round(weight, 4),
            "ok":         ok,
            "error":      res.get("error"),
        })

    return _envelope({
        "ok":          True,
        "etf":         f"{etf_sym} {etf_name}".strip(),
        "date":        date,
        "n_written":   n_ok,
        "n_failed":    len(holdings) - n_ok,
        "details":     results,
        "hint":        ("如果 n_failed > 0,請確認 Sheet 有「ETF快照」分頁、Apps Script 已"
                        "重新部署最新版(含 add_etf_snapshot 動作)。"),
    })


@tool(
    "get_watchlist",
    "**讀「追蹤清單」分頁** — 回傳使用者目前在關注但還沒買的所有股票。"
    "使用者問「我在追蹤什麼?」「我的觀察清單?」「我準備買什麼?」用這個。",
    {"type": "object", "properties": {}, "required": []},
)
async def get_watchlist(args: dict) -> dict:
    rows = sheets.load_watchlist()
    if rows and rows[0].get("_error"):
        return _envelope({"error": rows[0]["_error"]})
    return _envelope({
        "n_items": len(rows),
        "items":   rows,
    })


@tool(
    "compute_target_sell_prices",
    "**算出每檔部位「要賣到多少」才能淨賺 X%(扣完手續費 + 證交稅之後)**。"
    "預設目標 0% / 5% / 10% / 15% / 20%。ETF 套 0.1% 稅,一般股套 0.3% 稅,"
    "手續費用 USER_FEE_RATE(預設 0.1425%、下限 NT$1)。"
    "也會抓即時價算「距離目標還差多少 %」。"
    "使用者問「我要賺 X% 要賣多少」「2330 回本價多少」「我的停利目標」用這個。",
    {
        "type": "object",
        "properties": {
            "symbol":      {"type": "string", "description": "選填,只算一檔;沒給就算全部部位"},
            "target_pcts": {
                "type": "array",
                "items": {"type": "number"},
                "description": "目標淨報酬率(用 5 表示 5%,不是 0.05)。預設 [0,5,10,15,20]",
            },
            "fee_rate":    {"type": "number", "description": "選填 override 手續費率"},
            "fee_min":     {"type": "number", "description": "選填 override 手續費下限"},
        },
        "required": [],
    },
)
async def compute_target_sell_prices(args: dict) -> dict:
    fee_rate = float((args or {}).get("fee_rate")
                     or os.getenv("USER_FEE_RATE", "0.001425"))
    fee_min  = float((args or {}).get("fee_min")
                     or os.getenv("USER_FEE_MIN", "1"))
    target_pcts = (args or {}).get("target_pcts") or [0, 5, 10, 15, 20]
    sym_filter = str((args or {}).get("symbol") or "").strip()

    positions = sheets.load_positions()
    if positions and positions[0].get("_error"):
        return _envelope({"error": positions[0]["_error"]})

    rows: list[dict] = []
    for p in positions:
        sym = str(p.get("symbol", "")).strip()
        if sym_filter and sym != sym_filter:
            continue
        shares = int(p.get("shares") or 0)
        total_cost = float(p.get("total_cost") or 0)
        if shares <= 0 or total_cost <= 0:
            continue

        # ETF (代號 00 開頭) 套 0.1%,一般股套 0.3%
        is_etf   = sym.startswith("00") and len(sym) >= 4
        tax_rate = 0.001 if is_etf else 0.003

        # 抓現價:Fugle 優先,失敗用 Sheet 上的 current_price
        price: float | None = None
        price_source = "none"
        try:
            q = _client.quote(sym)
            for k in ("lastPrice", "closePrice", "price", "referencePrice"):
                v = q.get(k) if isinstance(q, dict) else None
                if v:
                    price = float(v)
                    price_source = f"fugle.{k}"
                    break
        except Exception:
            pass
        if not price:
            manual = p.get("current_price")
            if manual:
                try:
                    price = float(manual)
                    price_source = "sheet.current_price"
                except (TypeError, ValueError):
                    price = None

        # 對每個目標 % 算對應賣價
        # 數學:net_proceeds = TC × (1 + P/100) = gross × (1 - fee_rate - tax_rate)
        # → gross = TC(1+P/100) / (1 - fee_rate - tax_rate)
        # → sell_price = gross / shares
        # 如果手續費小於 NT$1 下限,改用 fee_min 版公式
        targets = []
        for pct in target_pcts:
            target_net = total_cost * (1 + pct / 100.0)
            # 先試「百分比手續費」版
            gross = target_net / (1 - fee_rate - tax_rate)
            actual_fee = gross * fee_rate
            if actual_fee < fee_min:
                # 手續費被下限蓋過 → 換公式重算
                gross = (target_net + fee_min) / (1 - tax_rate)
            sell_price = gross / shares
            dist = ((sell_price - price) / price * 100) if (price and price > 0) else None
            targets.append({
                "target_pct": pct,
                "sell_price": round(sell_price, 2),
                "distance_from_current_pct": round(dist, 2) if dist is not None else None,
            })

        rows.append({
            "symbol":         sym,
            "name":           p.get("name", ""),
            "shares":         shares,
            "total_cost":     round(total_cost, 2),
            "cost_per_share": round(total_cost / shares, 4),
            "current_price":  round(price, 4) if price else None,
            "price_source":   price_source,
            "is_etf":         is_etf,
            "tax_rate":       tax_rate,
            "targets":        targets,
        })

    return _envelope({
        "ok":            True,
        "mode":          _client.mode,
        "fee_rate_used": fee_rate,
        "fee_min_used":  fee_min,
        "n_positions":   len(rows),
        "positions":     rows,
        "note": (
            "公式:目標賣價 = (總成本 × (1 + 目標%)) ÷ (股數 × (1 - 手續費率 - 證交稅率))。"
            "賣到該價會「實際進你戶頭」剛好等於對應的淨報酬率。"
            "ETF 因為證交稅只有 0.1%(一般股 0.3%),目標賣價會比一般股低一點點(對你有利)。"
        ),
    })


# =============================================================================
# 🎯 「整理一下」工作流 — 對追蹤清單 / 股票部位每一檔跑 K 線分析 + AI 建議
# =============================================================================

def _compute_signals(sym: str) -> dict:
    """對單一代號抓 ~270 個交易日 K 線、跑 RSI/SMA/52w high。
    回傳 5 個訊號 + 白話訊號摘要。失敗時 ok=False。"""
    try:
        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=400)  # cal days,留 buffer 給休市
        data = _client.candles(sym,
                                from_date=from_dt.strftime("%Y-%m-%d"),
                                to_date=to_dt.strftime("%Y-%m-%d"))
        bars = data.get("data", [])
        if len(bars) < 60:
            return {"ok": False, "error": f"K 線只 {len(bars)} 根,不夠算 60MA"}
        closes = [b["close"] for b in bars]

        # 抓即時價:Fugle quote 優先,失敗用最後一根 close
        current = closes[-1]
        try:
            q = _client.quote(sym)
            for k in ("lastPrice", "closePrice", "price",
                      "referencePrice", "previousClose"):
                v = q.get(k) if isinstance(q, dict) else None
                if v:
                    current = float(v)
                    break
        except Exception:
            pass

        rsi14 = rsi(closes, 14)[-1]
        ma20 = sma(closes, 20)[-1]
        ma60 = sma(closes, 60)[-1]
        dist_20ma_pct = (current / ma20 - 1) * 100 if ma20 else 0
        dist_60ma_pct = (current / ma60 - 1) * 100 if ma60 else 0
        change_5d_pct = (current / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
        window = closes[-252:] if len(closes) >= 252 else closes   # 52 週 ≈ 252 個交易日
        high_52w = max(window)
        dist_52w_high_pct = (current / high_52w - 1) * 100 if high_52w else 0

        return {
            "ok": True,
            "current_price":     round(current, 2),
            "rsi14":             round(rsi14, 1),
            "dist_20ma_pct":     round(dist_20ma_pct, 2),
            "dist_60ma_pct":     round(dist_60ma_pct, 2),
            "change_5d_pct":     round(change_5d_pct, 2),
            "dist_52w_high_pct": round(dist_52w_high_pct, 2),
            "summary":           _signal_summary_zh(rsi14, dist_20ma_pct, dist_60ma_pct,
                                                     change_5d_pct, dist_52w_high_pct),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _signal_summary_zh(rsi14, dist_20ma, dist_60ma, change_5d, dist_52w_high) -> str:
    """把 5 個訊號翻譯成白話一句話(不出現專有名詞,給使用者直接看)。"""
    parts = []
    # RSI → 「大家在買還是在賣」
    if rsi14 >= 70:
        parts.append("大家在搶買、可能過熱")
    elif rsi14 >= 55:
        parts.append("買的人較多")
    elif rsi14 >= 45:
        parts.append("買賣勢均力敵")
    elif rsi14 >= 30:
        parts.append("賣的人較多")
    else:
        parts.append("大家都在賣、可能殺過頭")

    # 短期均線位置
    if dist_20ma < -3:
        parts.append("短期跌深")
    elif dist_20ma < 0:
        parts.append("短期跌破均線")
    elif dist_20ma <= 3:
        parts.append("貼近短期均線")
    else:
        parts.append("短期漲很多")

    # 中期均線(只在轉弱時才提)
    if dist_60ma < -5:
        parts.append("中期趨勢已壞")
    elif dist_60ma < 0:
        parts.append("中期略弱")

    # 近 5 日漲跌
    if change_5d <= -5:
        parts.append(f"這週跌很慘({change_5d:.1f}%)")
    elif change_5d <= -2:
        parts.append(f"最近在跌({change_5d:.1f}%)")
    elif change_5d >= 5:
        parts.append(f"這週漲很多(+{change_5d:.1f}%)")
    elif change_5d >= 2:
        parts.append(f"最近在漲(+{change_5d:.1f}%)")
    else:
        parts.append("最近價格沒什麼動")

    # 距 52 週高
    if dist_52w_high >= -3:
        parts.append("貼近一年新高")
    elif dist_52w_high >= -10:
        parts.append(f"離一年高點 {-dist_52w_high:.0f}%")
    elif dist_52w_high >= -25:
        parts.append(f"離一年高點滿遠({-dist_52w_high:.0f}%)")
    else:
        parts.append(f"從高點摔很慘(−{-dist_52w_high:.0f}%)")

    return "、".join(parts)


def _watchlist_verdict(signals: dict) -> str:
    """追蹤清單 AI 建議規則(固定不變,確保每次答案一致):
        ❌ 不建議:RSI ≥ 70 或 近5日漲 ≥ 5%
        ✅ 可進場:(RSI ≤ 35 且現價跌破 20MA) 或 近5日跌 ≥ 5%
        🟡 觀察:其他"""
    if not signals.get("ok"):
        return "—"
    rsi_ = signals["rsi14"]
    dist_20ma = signals["dist_20ma_pct"]
    change_5d = signals["change_5d_pct"]
    if rsi_ >= 70 or change_5d >= 5:
        return "❌ 不建議"
    if (rsi_ <= 35 and dist_20ma < 0) or change_5d <= -5:
        return "✅ 可進場"
    return "🟡 觀察"


def _position_verdict(signals: dict, pnl_pct: float) -> str:
    """股票部位 AI 建議規則(固定不變):
        🔴 全部停利:損益 ≥ 20%
        🟠 停利 1/2:損益 ≥ 10%
        🟡 停利 1/4:損益 ≥ 5%
        ❌ 考慮停損:損益 ≤ -10% 且 RSI ≥ 45(沒超賣訊號可期反彈)
        🟢 可加碼:  損益 ≤ -5% 且 RSI ≤ 35
        ⚠️ 警戒:    RSI ≥ 70 或 跌破 60MA
        🔵 續抱:    其他"""
    if not signals.get("ok"):
        return "—"
    rsi_ = signals["rsi14"]
    dist_60ma = signals["dist_60ma_pct"]

    if pnl_pct >= 20:
        return "🔴 全部停利"
    if pnl_pct >= 10:
        return "🟠 停利 1/2"
    if pnl_pct >= 5:
        return "🟡 停利 1/4"
    if pnl_pct <= -10 and rsi_ >= 45:
        return "❌ 考慮停損"
    if pnl_pct <= -5 and rsi_ <= 35:
        return "🟢 可加碼"
    if rsi_ >= 70 or dist_60ma < -5:
        return "⚠️ 警戒"
    return "🔵 續抱"


def _organize_watchlist_inner() -> dict:
    """跑追蹤清單整理,回傳 dict(不包 _envelope)。"""
    rows = sheets.load_watchlist()
    if rows and rows[0].get("_error"):
        return {"error": rows[0]["_error"]}

    organized_at = _now_tw_str()
    results = []
    for row in rows:
        sym = str(row.get("symbol") or row.get("代號") or "").strip()
        if not sym:
            continue

        name = str(row.get("name") or row.get("名稱") or "").strip()
        if not name:
            name = _lookup_stock_name(sym)

        signals = _compute_signals(sym)
        if not signals.get("ok"):
            results.append({"symbol": sym, "name": name, "error": signals.get("error")})
            continue

        verdict = _watchlist_verdict(signals)
        payload = {
            "symbol":     sym,                   "代號":       sym,
            "name":       name,                  "名稱":       name,
            "現價":        signals["current_price"],
            "RSI(14)":    signals["rsi14"],
            "距20MA":      f"{signals['dist_20ma_pct']:+.2f}%",
            "距60MA":      f"{signals['dist_60ma_pct']:+.2f}%",
            "近5日漲跌":   f"{signals['change_5d_pct']:+.2f}%",
            "距52週高":    f"{signals['dist_52w_high_pct']:+.2f}%",
            "訊號摘要":    signals["summary"],
            "AI 建議":    verdict,
            "上次整理":    organized_at,
        }
        wb = sheets_writer.upsert_watchlist_item(**payload)

        results.append({
            "symbol":           sym,
            "name":             name,
            "current_price":    signals["current_price"],
            "rsi14":            signals["rsi14"],
            "dist_20ma_pct":    signals["dist_20ma_pct"],
            "dist_60ma_pct":    signals["dist_60ma_pct"],
            "change_5d_pct":    signals["change_5d_pct"],
            "dist_52w_high_pct": signals["dist_52w_high_pct"],
            "summary":          signals["summary"],
            "verdict":          verdict,
            "written":          bool(wb.get("ok")),
        })

    can_enter = [r for r in results if r.get("verdict", "").startswith("✅")]
    return {
        "ok":           True,
        "organized_at": organized_at,
        "n_items":      len(results),
        "items":        results,
        "n_can_enter":  len(can_enter),
        "can_enter":    can_enter,
    }


def _organize_positions_inner(fee_rate: float, fee_min: float) -> dict:
    """跑股票部位整理,回傳 dict(不包 _envelope)。"""
    organized_at = _now_tw_str()
    positions = sheets.load_positions()
    if positions and positions[0].get("_error"):
        return {"error": positions[0]["_error"]}

    results = []
    sum_cost = sum_market = sum_net = 0.0

    for p in positions:
        sym = str(p.get("symbol", "")).strip()
        shares = int(p.get("shares") or 0)
        total_cost = float(p.get("total_cost") or 0)
        name = str(p.get("name") or "").strip() or _lookup_stock_name(sym)

        if shares <= 0 or total_cost <= 0:
            continue

        signals = _compute_signals(sym)
        if not signals.get("ok"):
            results.append({"symbol": sym, "name": name, "error": signals.get("error")})
            continue

        price = signals["current_price"]
        is_etf = sym.startswith("00") and len(sym) >= 4
        tax_rate = 0.001 if is_etf else 0.003
        gross = price * shares
        fee = max(fee_min, gross * fee_rate)
        tax = gross * tax_rate
        net = gross - fee - tax
        pnl = net - total_cost
        pnl_pct = (pnl / total_cost * 100) if total_cost else 0

        sum_cost   += total_cost
        sum_market += gross
        sum_net    += net

        verdict = _position_verdict(signals, pnl_pct)
        payload = {
            "symbol":     sym,                  "代號":       sym,
            "name":       name,                 "名稱":       name,
            "現價":        round(price, 2),
            "市值":        round(gross, 2),
            "損益":        round(pnl, 2),
            "損益%":       round(pnl_pct, 2),
            "RSI(14)":    signals["rsi14"],
            "距20MA":      f"{signals['dist_20ma_pct']:+.2f}%",
            "距60MA":      f"{signals['dist_60ma_pct']:+.2f}%",
            "近5日漲跌":   f"{signals['change_5d_pct']:+.2f}%",
            "距52週高":    f"{signals['dist_52w_high_pct']:+.2f}%",
            "訊號摘要":    signals["summary"],
            "AI 建議":    verdict,
            "上次整理":    organized_at,
        }
        wb = sheets_writer.upsert_position(**payload)

        results.append({
            "symbol":         sym,
            "name":           name,
            "shares":         shares,
            "total_cost":     round(total_cost, 2),
            "current_price":  round(price, 2),
            "market_value":   round(gross, 2),
            "net_proceeds":   round(net, 2),
            "unrealized_pnl": round(pnl, 2),
            "pnl_pct":        round(pnl_pct, 2),
            "rsi14":          signals["rsi14"],
            "dist_20ma_pct":  signals["dist_20ma_pct"],
            "dist_60ma_pct":  signals["dist_60ma_pct"],
            "change_5d_pct":  signals["change_5d_pct"],
            "dist_52w_high_pct": signals["dist_52w_high_pct"],
            "summary":        signals["summary"],
            "verdict":        verdict,
            "written":        bool(wb.get("ok")),
        })

    return {
        "ok":           True,
        "organized_at": organized_at,
        "n_positions":  len(results),
        "positions":    results,
        "summary": {
            "total_cost":    round(sum_cost, 2),
            "total_market":  round(sum_market, 2),
            "total_net":     round(sum_net, 2),
            "total_pnl":     round(sum_net - sum_cost, 2),
            "total_pnl_pct": round((sum_net - sum_cost) / sum_cost * 100, 2) if sum_cost else 0,
        },
    }


@tool(
    "organize_watchlist",
    "**「整理一下追蹤清單」一鍵工作流** — 對「追蹤清單」每一檔自動抓 K 線、算 5 個技術訊號"
    "(RSI / 距20MA / 距60MA / 近5日漲跌 / 距52週高)、寫白話訊號摘要 + AI 建議"
    "(✅ 可進場 / 🟡 觀察 / ❌ 不建議),全部寫回 Sheet 對應 row。"
    "使用者說「整理一下」「我的觀察清單怎樣」「追蹤的股票看一下」「最近哪些可以買」用這個。"
    "回傳每檔明細 + can_enter(✅ 可進場的子集)。把 can_enter 列在最上面給使用者看,"
    "Markdown 表呈現:代號 / 名稱 / 現價 / 訊號摘要 / AI 建議。",
    {"type": "object", "properties": {}, "required": []},
)
async def organize_watchlist(args: dict) -> dict:
    return _envelope(_organize_watchlist_inner())


@tool(
    "organize_positions",
    "**「整理一下我的部位」一鍵工作流** — 對「股票部位」每一檔抓即時價 + 算市值/損益/損益% + "
    "跑 5 個技術訊號 + 寫白話訊號摘要 + AI 建議(🔴 全部停利 / 🟠 停利 1/2 / 🟡 停利 1/4 / "
    "🟢 可加碼 / ❌ 考慮停損 / ⚠️ 警戒 / 🔵 續抱),全部寫回 Sheet。"
    "使用者說「整理一下我的部位」「我的股票怎樣」「我該動哪些」「該不該停利」用這個。"
    "回傳每檔明細 + summary(總成本/市值/損益)。把需要動作的列(🔴/🟠/🟡/❌)提到最上面,"
    "Markdown 表呈現:代號 / 名稱 / 損益% / 訊號摘要 / AI 建議。",
    {
        "type": "object",
        "properties": {
            "fee_rate": {"type": "number", "description": "選填 override 手續費率"},
            "fee_min":  {"type": "number", "description": "選填 override 手續費下限"},
        },
        "required": [],
    },
)
async def organize_positions(args: dict) -> dict:
    fee_rate = float((args or {}).get("fee_rate")
                     or os.getenv("USER_FEE_RATE", "0.001425"))
    fee_min  = float((args or {}).get("fee_min")
                     or os.getenv("USER_FEE_MIN", "1"))
    return _envelope(_organize_positions_inner(fee_rate, fee_min))


@tool(
    "organize_all",
    "**「整理一下」終極一鍵** — 同時整理股票部位 + 追蹤清單。"
    "使用者單獨說「整理一下」「整理」「幫我看一下」(沒指明部位還是追蹤)時直接用這個。"
    "回傳 positions(股票部位整理結果)+ watchlist(追蹤清單整理結果),"
    "請分兩段 Markdown 表呈現:先股票部位(需要動作的提到最上),"
    "再追蹤清單(✅ 可進場的提到最上)。",
    {"type": "object", "properties": {}, "required": []},
)
async def organize_all(args: dict) -> dict:
    fee_rate = float(os.getenv("USER_FEE_RATE", "0.001425"))
    fee_min  = float(os.getenv("USER_FEE_MIN", "1"))
    pos = _organize_positions_inner(fee_rate, fee_min)
    wl  = _organize_watchlist_inner()
    return _envelope({
        "ok":         True,
        "positions":  pos,
        "watchlist":  wl,
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
    backfill_position_names,
    backfill_fund_names,
    valuate_portfolio,
    sync_portfolio_from_trades,
    get_realized_pnl,
    record_etf_snapshot,
    get_watchlist,
    organize_watchlist,
    organize_positions,
    organize_all,
    compute_target_sell_prices,
    ping_sheets_writer,
    get_us_quote,
    get_us_candles,
    get_stock_news,
    get_etf_holdings,
    search_taiwan_symbol,
    get_quote,
    get_candles,
    get_intraday_ticks,
    get_market_movers,
    compute_indicators,
    backtest_sma_crossover,
    backtest_rsi_mean_reversion,
]
