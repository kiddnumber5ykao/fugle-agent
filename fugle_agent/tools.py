# ⬆️【要上傳 2026-06-05 00:21】tools.py — 公司面免費資料+走勢敏感+公司簡介+上櫃營收待補
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
import time
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
from . import free_fetch
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

_NAME_CACHE: dict[str, str] = {}  # 同 session 同個 symbol 只查一次


def _lookup_stock_name(symbol: str) -> str:
    """查股票名稱,多層 fallback:內建熱門表 → Fugle tickers → Fugle quote(重試) → yfinance。
    重要:查不到就**不寫快取**,這樣同一檔之後還會再重試(避免一次失敗就永遠空白)。
    免費方案的 Fugle 雖然 tickers 拿不到,但 quote 一定會帶名字。"""
    import time as _time

    if not symbol:
        return ""
    # 只有「查到過名字」才用快取;空值不快取,留給下次重試
    if _NAME_CACHE.get(symbol):
        return _NAME_CACHE[symbol]

    name = ""

    # 1) 內建熱門表 + Fugle tickers(symbol_lookup.search)
    try:
        hits = symbol_lookup.search(symbol, fugle_client=_client, limit=5)
        for h in hits:
            if str(h.get("symbol")) == str(symbol):
                name = h.get("name") or ""
                break
        if not name and hits:
            name = hits[0].get("name", "")
    except Exception:
        pass

    # 2) Fugle quote 通常會帶 name 欄位 — 撞到 429 / 暫時失敗就重試最多 3 次
    if not name:
        for attempt in range(3):
            try:
                q = _client.quote(symbol)
                if isinstance(q, dict):
                    for key in ("name", "nameZhTw", "Name", "shortName"):
                        v = q.get(key)
                        if v:
                            name = str(v).strip()
                            break
                if name:
                    break
            except Exception:
                pass
            if attempt < 2:
                _time.sleep(2 * (attempt + 1))   # 2s, 4s 退避

    # 3) yfinance 最後備援(.TW / .TWO)— 連 Fugle 都查不到的冷門股
    if not name:
        try:
            import yfinance as _yf
            for suffix in (".TW", ".TWO"):
                try:
                    info = _yf.Ticker(f"{symbol}{suffix}").info or {}
                    v = info.get("longName") or info.get("shortName")
                    if v:
                        name = str(v).strip()
                        break
                except Exception:
                    continue
        except Exception:
            pass

    # 只在「真的查到」時才寫快取
    if name:
        _NAME_CACHE[symbol] = name
    return name


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
    """對單一代號抓 K 線、跑 RSI/SMA/52w high,降級回傳:
    - 至少 14 根 K 線才能算 RSI(必要,沒有就 ok=False)
    - 60MA 需要 60 根 → 不夠就回 None,訊號摘要省略中期均線
    - 52 週高需要最多 252 根 → 不夠就用現有 window 算
    這樣 Fugle 免費方案(只給 1 個月 ~21 根)也能跑大部分訊號。"""
    try:
        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=400)  # cal days,留 buffer 給休市
        data = _client.candles(sym,
                                from_date=from_dt.strftime("%Y-%m-%d"),
                                to_date=to_dt.strftime("%Y-%m-%d"))
        bars = data.get("data", [])
        if len(bars) < 14:
            return {"ok": False, "mode": _client.mode,
                    "error": f"K 線只 {len(bars)} 根(模式: {_client.mode}),不夠算 RSI"}
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

        # RSI(14):必有
        rsi14 = rsi(closes, 14)[-1]

        # 20MA:14 ~ 20 根可用前面所有 close 平均當近似;>= 20 根用標準 20MA
        if len(closes) >= 20:
            ma20 = sma(closes, 20)[-1]
        else:
            ma20 = sum(closes) / len(closes)
        dist_20ma_pct = (current / ma20 - 1) * 100 if ma20 else 0

        # 60MA:沒 60 根就回 None,訊號摘要省略中期均線
        ma60 = sma(closes, 60)[-1] if len(closes) >= 60 else None
        dist_60ma_pct = (current / ma60 - 1) * 100 if ma60 else None

        # 近 5 日漲跌
        change_5d_pct = (current / closes[-6] - 1) * 100 if len(closes) >= 6 else 0

        # 52 週高:不夠 252 根就用現有最高(會標註在訊號摘要)
        window = closes[-252:] if len(closes) >= 252 else closes
        high_window_label = "一年" if len(closes) >= 252 else f"近 {len(closes)} 日"
        high_52w = max(window)
        dist_52w_high_pct = (current / high_52w - 1) * 100 if high_52w else 0

        return {
            "ok":                True,
            "mode":              _client.mode,
            "n_bars":            len(bars),
            "current_price":     round(current, 2),
            "rsi14":             round(rsi14, 1),
            "dist_20ma_pct":     round(dist_20ma_pct, 2),
            "dist_60ma_pct":     round(dist_60ma_pct, 2) if dist_60ma_pct is not None else None,
            "change_5d_pct":     round(change_5d_pct, 2),
            "dist_52w_high_pct": round(dist_52w_high_pct, 2),
            "summary":           _signal_summary_zh(rsi14, dist_20ma_pct, dist_60ma_pct,
                                                     change_5d_pct, dist_52w_high_pct,
                                                     high_window_label),
        }
    except Exception as e:
        return {"ok": False, "mode": _client.mode, "error": f"{type(e).__name__}: {e}"}


def _signal_summary_zh(rsi14, dist_20ma, dist_60ma, change_5d, dist_52w_high,
                        high_window_label="一年") -> str:
    """白話訊號摘要,**每段都明確標時間框架**,讓使用者知道是「近兩週」還是「這週」。
    dist_60ma 可以是 None(資料不足時)。"""
    parts = []
    # RSI(14) → 「近兩週」買賣強弱
    if rsi14 >= 70:
        parts.append("近兩週買方主導、可能過熱")
    elif rsi14 >= 55:
        parts.append("近兩週買的人較多")
    elif rsi14 >= 45:
        parts.append("近兩週買賣勢均力敵")
    elif rsi14 >= 30:
        parts.append("近兩週賣的人較多")
    else:
        parts.append("近兩週賣方主導、可能殺過頭")

    # 距 20MA → 「短期 20 日內」價格位置
    if dist_20ma < -3:
        parts.append("短期(20日)跌深")
    elif dist_20ma < 0:
        parts.append("短期(20日)跌破均線")
    elif dist_20ma <= 3:
        parts.append("貼近 20 日均線")
    else:
        parts.append("短期(20日)強勢")

    # 距 60MA → 「中期 60 日內」價格位置
    if dist_60ma is not None:
        if dist_60ma < -5:
            parts.append("中期(60日)趨勢已壞")
        elif dist_60ma < 0:
            parts.append("中期(60日)略弱")

    # 近 5 日 → 「這週」
    if change_5d <= -5:
        parts.append(f"這週跌得多({change_5d:.1f}%)")
    elif change_5d <= -2:
        parts.append(f"這週在跌({change_5d:.1f}%)")
    elif change_5d >= 5:
        parts.append(f"這週漲得多(+{change_5d:.1f}%)")
    elif change_5d >= 2:
        parts.append(f"這週在漲(+{change_5d:.1f}%)")
    else:
        parts.append("這週價格沒什麼動")

    # 距 52 週高 → 「{一年/近 N 日}」相對位置
    if dist_52w_high >= -3:
        parts.append(f"貼近{high_window_label}新高")
    elif dist_52w_high >= -10:
        parts.append(f"離{high_window_label}高點 {-dist_52w_high:.0f}%")
    elif dist_52w_high >= -25:
        parts.append(f"離{high_window_label}高點滿遠({-dist_52w_high:.0f}%)")
    else:
        parts.append(f"從{high_window_label}高點摔很慘(−{-dist_52w_high:.0f}%)")

    return "、".join(parts)


def _outlook_verdict(signals: dict) -> str:
    """**純技術面、前瞻性的展望分析** — 看「接下來這檔還有沒有戲」,跟使用者的損益/成本
    完全脫鉤。股票部位、追蹤清單共用同一套邏輯。

    採「動能(momentum)」框架,不採「均值回歸」 — 訊號偏空就回 🔴 看衰(不會反過來說
    跌深 = 進場機會)。

    給三燈號:
        🟢 看好 — 價格在往上走、買的人多、最近漲得不錯
        🟡 中性 — 沒明顯方向、上下都有可能、訊號混雜
        🔴 看衰 — 價格在往下走、賣的人多、最近跌得不少

    使用者拿這個 + 自己的損益,**自己**判斷要不要動。"""
    if not signals.get("ok"):
        return "—"

    score = 0
    rsi_ = signals["rsi14"]
    dist_20ma = signals["dist_20ma_pct"]
    dist_60ma = signals["dist_60ma_pct"]
    change_5d = signals["change_5d_pct"]
    dist_52w = signals["dist_52w_high_pct"]

    # ── RSI (近兩週買賣強弱) — 動能視角 ──────────────────
    if rsi_ >= 60:
        score += 2
    elif rsi_ >= 50:
        score += 1
    elif rsi_ >= 40:
        score += 0           # 中性
    elif rsi_ >= 30:
        score -= 1
    else:
        score -= 2           # 賣方主導

    # ── 短期均線 20MA 位置 ───────────────────────────────
    if dist_20ma >= 3:
        score += 2
    elif dist_20ma >= 0:
        score += 1
    elif dist_20ma >= -3:
        score -= 1
    else:
        score -= 2

    # ── 中期均線 60MA 位置(資料夠才看) ──────────────────
    if dist_60ma is not None:
        if dist_60ma >= 5:
            score += 2
        elif dist_60ma >= 0:
            score += 1
        elif dist_60ma >= -5:
            score -= 1
        else:
            score -= 2

    # ── 5 日動能 ──────────────────────────────────────────
    if change_5d >= 5:
        score += 2
    elif change_5d >= 2:
        score += 1
    elif change_5d <= -5:
        score -= 2
    elif change_5d <= -2:
        score -= 1

    # ── 距 52 週高(相對強弱) ────────────────────────────
    if dist_52w >= -5:
        score += 1
    elif dist_52w >= -15:
        score += 0
    elif dist_52w >= -25:
        score -= 1
    else:
        score -= 2

    # ── Score 範圍約 -9 ~ +9,翻譯成三燈號 ─────────────
    if score >= 4:
        return "🟢 看好"
    if score >= -1:
        return "🟡 中性"
    return "🔴 看衰"


# 保留舊名稱當 alias,避免其他地方 import 壞掉
_watchlist_verdict = _outlook_verdict
_position_verdict = lambda signals, pnl_pct=None: _outlook_verdict(signals)


def _fmt_pct(v) -> str:
    """格式化百分比，None 顯示為「—」(資料不足時用)。"""
    if v is None:
        return "—"
    return f"{v:+.2f}%"


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
            "距20MA":      _fmt_pct(signals["dist_20ma_pct"]),
            "距60MA":      _fmt_pct(signals["dist_60ma_pct"]),
            "近5日漲跌":   _fmt_pct(signals["change_5d_pct"]),
            "距52週高":    _fmt_pct(signals["dist_52w_high_pct"]),
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
            "距20MA":      _fmt_pct(signals["dist_20ma_pct"]),
            "距60MA":      _fmt_pct(signals["dist_60ma_pct"]),
            "近5日漲跌":   _fmt_pct(signals["change_5d_pct"]),
            "距52週高":    _fmt_pct(signals["dist_52w_high_pct"]),
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


# =============================================================================
# 🎯 短線分析工作流 v2 — 技術面 + 基本面雙軌設計 (5 燈號 + 綜合建議)
# =============================================================================
# 觸發詞:
#   「整體技術分析」→ organize_all_technical(只跑技術,30~60 秒)
#   「整體深度分析」→ organize_all_deep   (技術 + 基本面,15~25 分)
# =============================================================================

def _compute_short_signals(sym: str) -> dict:
    """短線版的訊號計算 — 對單一代號抓 K 線 + 量,
    產出 6 段白話描述 + 內部數值給 2 個技術燈號計算用。
    內建 429 重試:遇到 Fugle rate limit 自動等 20 秒再試一次。"""
    try:
        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=400)
        # 🛡️ 自帶 429 重試 — Fugle 流量管制下會 burst 失敗,等 20 秒讓配額重置
        data = None
        for attempt in range(3):
            try:
                data = _client.candles(sym,
                                        from_date=from_dt.strftime("%Y-%m-%d"),
                                        to_date=to_dt.strftime("%Y-%m-%d"))
                break
            except Exception as e:
                err_s = str(e)
                if ("429" in err_s or "rate limit" in err_s.lower()) and attempt < 2:
                    print(f"⏳ {sym} Fugle 429,等 {20 * (attempt + 1)} 秒重試",
                          flush=True)
                    time.sleep(20 * (attempt + 1))   # 20s, 40s 漸進
                    continue
                # 其他錯誤直接放棄
                return {"ok": False, "mode": _client.mode,
                        "error": f"{type(e).__name__}: {e}"}
        if data is None:
            return {"ok": False, "mode": _client.mode,
                    "error": "Fugle 重試 3 次後仍失敗"}
        bars = data.get("data", [])
        if len(bars) < 7:
            return {"ok": False, "mode": _client.mode,
                    "error": f"K 線只 {len(bars)} 根、不夠分析"}
        # 🛡️ 強制按日期升冪排序 — Fugle 不保證回傳順序,有些股票會反序
        # 排序後 bars[-1] 一定是最新一根
        bars = sorted(bars, key=lambda b: str(b.get("date", "")).strip())
        closes = [b["close"] for b in bars]
        volumes = [b.get("volume", 0) for b in bars]

        # 🌟 抓「最新價格」+「昨日收盤」做比較
        # 策略(從最新到最舊):
        #   1) Fugle quote.lastPrice  — 盤中即時(最新!)
        #   2) yfinance regularMarketPrice — Fugle 沒抓到時的備援(延遲 15-20 分)
        #   3) candles[-1] close — 都失敗就用最後一根 K 線

        _tw_now = datetime.now(timezone(timedelta(hours=8)))
        today_str = _tw_now.strftime("%Y-%m-%d")

        # 找「昨日」收盤:最後一根日期 < 今天的 bar
        prev_close = None
        prev_date = ""
        for bar in reversed(bars):
            bd = str(bar.get("date", "")).strip()[:10]
            if bd and bd < today_str:
                prev_close = bar.get("close")
                prev_date = bd
                break
        # 如果今天 bar 不存在,closes[-1] 本身就是昨日 → 用 closes[-2] 當前天
        if prev_close is None:
            prev_close = closes[-2] if len(closes) >= 2 else closes[-1]
            prev_date = (str(bars[-2].get("date", ""))[:10]
                         if len(bars) >= 2 else "(無)")

        # ① 抓 Fugle quote 即時價
        current = None
        current_source = ""
        current_date_assumed = ""
        try:
            q = _client.quote(sym)
            if isinstance(q, dict):
                for k in ("lastPrice", "closePrice", "price"):
                    v = q.get(k)
                    if v:
                        try:
                            current = float(v)
                            current_source = f"fugle_quote.{k}"
                            # quote 給的價跟昨日不同 → 是今天的盤中/收盤
                            if abs(current - prev_close) > 0.001:
                                current_date_assumed = today_str
                            else:
                                # 一模一樣 → 可能 Fugle 還沒更新今天 → 標昨日
                                current_date_assumed = prev_date
                            break
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass

        # ② 如果 Fugle 沒給今天的價(current 還是 None 或等於昨日),用 yfinance 試
        if current is None or (current == prev_close and current_date_assumed != today_str):
            try:
                import yfinance as _yf
                # 台股加 .TW(主板)或 .TWO(櫃買)後綴
                for suffix in (".TW", ".TWO"):
                    try:
                        tk = _yf.Ticker(f"{sym}{suffix}")
                        info = tk.fast_info if hasattr(tk, "fast_info") else {}
                        yf_price = info.get("last_price") or info.get("lastPrice")
                        if yf_price is None:
                            # fallback to history
                            hist = tk.history(period="1d", interval="1m")
                            if not hist.empty:
                                yf_price = float(hist["Close"].iloc[-1])
                        if yf_price and float(yf_price) > 0:
                            current = float(yf_price)
                            current_source = f"yfinance{suffix}"
                            current_date_assumed = today_str
                            break
                    except Exception:
                        continue
            except ImportError:
                pass

        # ③ 兩個都失敗就用最後一根 K 線
        if current is None:
            current = closes[-1]
            current_source = "candles[-1]"
            current_date_assumed = str(bars[-1].get("date", "")).strip()[:10]

        # 🌟 校正比較基準:讓「最新表現」永遠是「最近一個交易日的真實漲跌」。
        # 假日/盤後 Fugle 沒給今天新價時,不會因為 current==最後一根收盤 而變「沒漲沒跌」,
        # 改成用「最後一個交易日收盤 vs 前一個交易日收盤」。
        _last_close = closes[-1]
        _last_date = str(bars[-1].get("date", "")).strip()[:10]
        if current_date_assumed == today_str and abs(current - _last_close) > 0.001:
            # 真的有今天盤中新價 → 跟最後一根(昨收)比
            prev_close = _last_close
            prev_date = _last_date
        else:
            # 沒有今天的新價(假日/盤後)→ 用最後一根收盤,跟前一根比
            current = _last_close
            current_source += "+用收盤"
            current_date_assumed = _last_date
            prev_close = closes[-2] if len(closes) >= 2 else _last_close
            prev_date = (str(bars[-2].get("date", "")).strip()[:10]
                         if len(bars) >= 2 else "")

        # 計算最近一個交易日表現:current vs prev_close
        today_change_pct = ((current / prev_close - 1) * 100 if prev_close else 0)
        latest_date = current_date_assumed
        today_vol = volumes[-1] if volumes else 0

        # Debug log
        first_bar_date = str(bars[0].get("date", "")).strip()[:10] if bars else ""
        last_bar_date = str(bars[-1].get("date", "")).strip()[:10] if bars else ""
        print(f"   📊 {sym} 最新={today_change_pct:+.2f}% | "
              f"current={current} ({current_source}, 假設日期={current_date_assumed}) | "
              f"prev_close={prev_close} ({prev_date}) | "
              f"bars=[{first_bar_date}~{last_bar_date}] n={len(bars)} | "
              f"TW={today_str}",
              flush=True)

        avg_vol_20 = (sum(volumes[-20:]) / min(len(volumes), 20)) if volumes else 0
        vol_ratio_today = (today_vol / avg_vol_20) if avg_vol_20 else 1.0
        if vol_ratio_today >= 1.8:
            vol_phrase = f"量比平常大 {vol_ratio_today:.1f} 倍"
        elif vol_ratio_today >= 1.3:
            vol_phrase = f"量稍大 ({vol_ratio_today:.1f} 倍)"
        elif vol_ratio_today <= 0.5:
            vol_phrase = "量很小"
        elif vol_ratio_today <= 0.8:
            vol_phrase = "量稍小"
        else:
            vol_phrase = "量正常"

        # 日期 label:今天 vs 「5/28」這種短格式
        if latest_date == today_str:
            when_label = "今天"
        elif latest_date:
            parts = latest_date.split("-")
            if len(parts) == 3:
                when_label = f"{int(parts[1])}/{int(parts[2])}"
            else:
                when_label = latest_date
        else:
            when_label = "最近"

        # 描述 — 不再說「平盤」,只在真的 ±0.05% 內才說「沒漲沒跌」
        if today_change_pct >= 2:
            today_desc = f"{when_label}漲 +{today_change_pct:.1f}%、{vol_phrase}"
        elif today_change_pct >= 0.5:
            today_desc = f"{when_label}微漲 +{today_change_pct:.1f}%、{vol_phrase}"
        elif today_change_pct >= 0.05:
            today_desc = f"{when_label}小漲 +{today_change_pct:.2f}%、{vol_phrase}"
        elif today_change_pct <= -2:
            today_desc = f"{when_label}跌 {today_change_pct:.1f}%、{vol_phrase}"
        elif today_change_pct <= -0.5:
            today_desc = f"{when_label}微跌 {today_change_pct:.1f}%、{vol_phrase}"
        elif today_change_pct <= -0.05:
            today_desc = f"{when_label}小跌 {today_change_pct:.2f}%、{vol_phrase}"
        else:
            today_desc = f"{when_label}沒漲沒跌、{vol_phrase}"

        # 2) 最近 3 天
        change_3d_pct = (current / closes[-4] - 1) * 100 if len(closes) >= 4 else 0
        if change_3d_pct >= 4:
            last3d_desc = f"連 3 天紅、漲了 +{change_3d_pct:.1f}%"
        elif change_3d_pct >= 1:
            last3d_desc = f"3 天上漲 +{change_3d_pct:.1f}%"
        elif change_3d_pct >= -1:
            last3d_desc = "3 天內漲跌互見、沒明顯方向"
        elif change_3d_pct >= -4:
            last3d_desc = f"3 天下跌 {change_3d_pct:.1f}%"
        else:
            last3d_desc = f"連 3 天黑、跌了 {change_3d_pct:.1f}%"

        # 3) 這週氛圍 (RSI7)
        rsi7 = rsi(closes, 7)[-1] if len(closes) >= 8 else 50
        if rsi7 >= 70:
            mood_desc = "最近一週很多人搶買、可能太熱"
        elif rsi7 >= 55:
            mood_desc = "最近一週買的人較多"
        elif rsi7 >= 45:
            mood_desc = "最近一週買賣勢均力敵"
        elif rsi7 >= 30:
            mood_desc = "最近一週賣的人較多"
        else:
            mood_desc = "最近一週很多人在拋售、可能跌過頭"

        # 4) 近 10 天走勢 (MA10)
        ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else sum(closes) / len(closes)
        dist_ma10_pct = (current / ma10 - 1) * 100 if ma10 else 0
        if dist_ma10_pct >= 3:
            ma10_desc = f"過去 10 天在漲、目前比平均高 {dist_ma10_pct:.1f}%"
        elif dist_ma10_pct >= 0:
            ma10_desc = f"過去 10 天溫和、目前略高於平均 (+{dist_ma10_pct:.1f}%)"
        elif dist_ma10_pct >= -3:
            ma10_desc = f"過去 10 天小弱、目前略低於平均 ({dist_ma10_pct:.1f}%)"
        else:
            ma10_desc = f"過去 10 天跌深、目前比平均低 {-dist_ma10_pct:.1f}%"

        # 5) 量能變化 (近 5 vs 過去 20)
        avg_vol_5 = (sum(volumes[-5:]) / 5) if len(volumes) >= 5 else (sum(volumes)/len(volumes) if volumes else 0)
        avg_vol_20_full = (sum(volumes[-20:]) / 20) if len(volumes) >= 20 else (sum(volumes)/len(volumes) if volumes else 0)
        vol_ratio_5_20 = (avg_vol_5 / avg_vol_20_full) if avg_vol_20_full else 1.0
        if vol_ratio_5_20 >= 1.5:
            if change_3d_pct >= 0:
                vol_change_desc = f"近 5 天明顯放量上漲、有人積極買進 ({vol_ratio_5_20:.1f} 倍)"
            else:
                vol_change_desc = f"近 5 天放量下跌、賣壓重 ({vol_ratio_5_20:.1f} 倍)"
        elif vol_ratio_5_20 >= 1.2:
            vol_change_desc = f"近 5 天量稍多、有資金進出 ({vol_ratio_5_20:.1f} 倍)"
        elif vol_ratio_5_20 <= 0.6:
            vol_change_desc = "近 5 天明顯縮量、市場關注度低"
        elif vol_ratio_5_20 <= 0.8:
            vol_change_desc = "近 5 天量稍少、觀望氣氛重"
        else:
            vol_change_desc = "量能正常、沒明顯變化"

        # 6) 離 20 天高/低點
        window20 = closes[-20:] if len(closes) >= 20 else closes
        high20 = max(window20)
        low20 = min(window20)
        dist_high20_pct = (current / high20 - 1) * 100 if high20 else 0
        dist_low20_pct = (current / low20 - 1) * 100 if low20 else 0
        if dist_high20_pct >= -2:
            range20_desc = "快摸到 20 天內最高點"
        elif dist_high20_pct >= -5:
            range20_desc = f"離 20 天最高點 {-dist_high20_pct:.1f}%"
        elif dist_low20_pct <= 2:
            range20_desc = "貼近 20 天內最低點"
        elif dist_low20_pct <= 5:
            range20_desc = f"接近 20 天最低點 (距底 +{dist_low20_pct:.1f}%)"
        else:
            range20_desc = (f"在 20 天區間中段 (距高 {dist_high20_pct:.1f}%、"
                            f"距低 +{dist_low20_pct:.1f}%)")

        return {
            "ok":               True,
            "mode":             _client.mode,
            "n_bars":           len(bars),
            "current_price":    round(current, 2),
            "latest_date":      latest_date,         # 給 Sheet「資料日期」欄用
            # White-language descriptions
            "today_desc":       today_desc,
            "last3d_desc":      last3d_desc,
            "weekly_mood_desc": mood_desc,
            "ma10_desc":        ma10_desc,
            "volume_desc":      vol_change_desc,
            "range20_desc":     range20_desc,
            # Raw numbers for verdict
            "today_change_pct": today_change_pct,
            "today_vol_ratio":  vol_ratio_today,
            "change_3d_pct":    change_3d_pct,
            "change_20d_pct":   ((current / closes[-21] - 1) * 100
                                 if len(closes) >= 21 else 0),
            "rsi7":             rsi7,
            "dist_ma10_pct":    dist_ma10_pct,
            "vol_ratio_5_20":   vol_ratio_5_20,
            "dist_high20_pct":  dist_high20_pct,
            "dist_low20_pct":   dist_low20_pct,
        }
    except Exception as e:
        return {"ok": False, "mode": getattr(_client, "mode", "unknown"),
                "error": f"{type(e).__name__}: {e}"}


_MKT_20D_CACHE: dict = {"ts": None, "val": None}


def _market_20d_return() -> float | None:
    """加權指數近 20 交易日報酬%(每次跑快取 10 分,避免每檔重抓)。"""
    import time
    now = time.time()
    if _MKT_20D_CACHE["ts"] and now - _MKT_20D_CACHE["ts"] < 600:
        return _MKT_20D_CACHE["val"]
    val = None
    try:
        from . import us_market
        r = us_market.candles("^TWII")
        closes = [b["close"] for b in (r or {}).get("data", []) if b.get("close")]
        if len(closes) >= 21:
            val = (closes[-1] / closes[-21] - 1) * 100
    except Exception:
        val = None
    _MKT_20D_CACHE.update(ts=now, val=val)
    return val


def _relative_strength(signals: dict) -> str:
    """個股近 20 天 vs 大盤近 20 天 → 比大盤強 / 差不多 / 比大盤弱(白話)。
    抓不到大盤時回空字串。"""
    if not signals.get("ok"):
        return ""
    mkt = _market_20d_return()
    if mkt is None:
        return ""
    diff = (signals.get("change_20d_pct", 0) or 0) - mkt
    if diff >= 5:
        return "比大盤強"
    if diff <= -5:
        return "比大盤弱"
    return "跟大盤差不多"


def _stop_price(signals: dict) -> float | None:
    """停損價 = 10 日線價位。用現價與『距10日線%』反推。"""
    if not signals.get("ok"):
        return None
    cur = signals.get("current_price")
    m = signals.get("dist_ma10_pct")
    if cur is None or m is None:
        return None
    try:
        return round(cur / (1 + m / 100.0), 2)
    except Exception:
        return None


def _short_term_light(signals: dict) -> str:
    """短線燈號 (A) — 5-10 天視角 — 用 RSI7 + MA10 + 3 天動能 + 20 天區間位置。"""
    if not signals.get("ok"):
        return "⚪ 資料不足"
    score = 0
    r = signals["rsi7"]
    if   r >= 60: score += 2
    elif r >= 50: score += 1
    elif r >= 40: score += 0
    elif r >= 30: score -= 1
    else:         score -= 2
    m = signals["dist_ma10_pct"]
    if   m >= 3:  score += 2
    elif m >= 0:  score += 1
    elif m >= -3: score -= 1
    else:         score -= 2
    c = signals["change_3d_pct"]
    if   c >= 4:  score += 2
    elif c >= 1:  score += 1
    elif c <= -4: score -= 2
    elif c <= -1: score -= 1
    dh = signals["dist_high20_pct"]
    dl = signals["dist_low20_pct"]
    if dh >= -3:  score += 1
    elif dl <= 3: score -= 1
    if score >= 3:  return "🟢 看好"
    if score >= -1: return "🟡 中性"
    return "🔴 看衰"


def _momentum_light(signals: dict) -> tuple[str, str]:
    """動能燈(五段)+ 白話原因。用 趨勢(離10日線)+ 速度(近3天)+ 量 算動能強弱,
    切 🔥強勢 / 🟢偏多 / 🟡中性 / 🟠偏弱 / 🔴弱勢,讓同色裡也分得出高低。
    回傳 (燈號, 像朋友講話的白話原因)。

    下游 _light_emoji 會把 🔥→🟢、🟠→🔴 收斂成 3 段去判「我該做啥」,
    所以顯示是 5 段、決策行為仍穩定。"""
    if not signals.get("ok"):
        return ("⚪ 資料不足", "抓不到股價資料,沒辦法看")
    m = signals.get("dist_ma10_pct", 0) or 0       # 距10日線%:>0 站上、<0 跌破(趨勢)
    t = signals.get("today_change_pct", 0) or 0     # 今天漲跌%(敏感主力)
    c = signals.get("change_3d_pct", 0) or 0        # 近3天漲跌%(阻尼)
    vr = signals.get("vol_ratio_5_20", 1.0) or 1.0  # 近期量 vs 平常量
    big_vol = vr >= 1.3
    low_vol = vr <= 0.7

    # 動能強弱分數:趨勢(離均價,當底)+ 速度(今天領頭、近3天當阻尼)
    # ★2026-06-04:把「今天」拆出來當速度主力,讓單日轉折當天就反應(敏感)。
    speed = t * 1.3 + c * 0.5
    trend = m * 0.5
    s = trend + speed
    if big_vol:
        s *= 1.25
    elif low_vol:
        s *= 0.75

    # 門檻略收緊(敏感):比舊版更早換色
    if s >= 4.0:
        light = "🔥 強勢"
    elif s >= 1.0:
        light = "🟢 偏多"
    elif s > -1.0:
        light = "🟡 中性"
    elif s > -4.0:
        light = "🟠 偏弱"
    else:
        light = "🔴 弱勢"

    # 過熱:已經是強勢、但股價離 10 日線太遠(漲多了)→ 標「(過熱)」,提醒別追高
    overheated = (light == "🔥 強勢" and m >= 10)
    if overheated:
        light = "🔥 強勢(過熱)"

    # 今天有大動作時,在白話原因前面點出來(跟燈號的即時反應對齊)
    if t >= 2:
        _today_lead = f"今天大漲 +{t:.1f}%,"
    elif t <= -2:
        _today_lead = f"今天大跌 {t:.1f}%,"
    else:
        _today_lead = ""

    # 白話原因(像朋友講)
    if light.startswith("🔥"):
        if overheated:
            reason = ("漲得又快又猛,但已經離均價很遠、短線漲多了,"
                      "要追小心追在高點,想買等拉回比較安全")
        else:
            tail = ("一堆人在搶買,氣勢很強" if big_vol
                    else ("量沒特別爆但走勢很猛" if low_vol else "氣勢很強"))
            reason = f"漲得又快又猛,股價衝在均價上面,{tail},看起來還在往上衝"
    elif light == "🟢 偏多":
        tail = "越來越多人進場" if big_vol else ("不過買的人沒特別多" if low_vol else "買盤穩穩的")
        reason = f"穩穩在漲,站在均價之上,{tail},看起來還會往上"
    elif light == "🟡 中性":
        if abs(t) >= 2:
            # 今天有大動作但整體還沒轉向 → 別說「沒什麼動」自打嘴巴
            where = "還站在均價上面" if m >= 0 else "還在均價下面"
            reason = f"但拉回整體看,{where}、方向還沒定,先看緊一點別急著動"
        elif m >= 0:
            reason = "還在均價之上,但這幾天沒什麼動,卡在那上上下下,先看看"
        else:
            reason = "最近卡在區間裡上上下下,看不出要往哪走,先看看"
    elif light == "🟠 偏弱":
        tail = ",賣的人在變多" if big_vol else (",不過賣壓還不大" if low_vol else "")
        reason = f"開始往下掉了{tail},雖然還沒真的破底,但要留意、先別急著追"
    else:  # 🔴 弱勢
        tail = ",賣壓還很重" if big_vol else (",不過賣壓沒爆" if low_vol else "")
        reason = f"跌得明顯、跌破了近期均價{tail},氣氛很弱,還沒看到止跌"
    return (light, _today_lead + reason)


def _super_short_term_light(signals: dict) -> str:
    """超短線燈號 (B) — 2-5 天視角 — 重今天表現 + 量 + 3 天動能。"""
    if not signals.get("ok"):
        return "⚪ 資料不足"
    score = 0
    t = signals["today_change_pct"]
    if   t >= 2:    score += 2
    elif t >= 0.5:  score += 1
    elif t <= -2:   score -= 2
    elif t <= -0.5: score -= 1
    vr = signals.get("today_vol_ratio", 1.0)
    if vr >= 1.5 and t > 0:  score += 1
    elif vr >= 1.5 and t < 0: score -= 1
    c = signals["change_3d_pct"]
    if   c >= 3:  score += 2
    elif c >= 1:  score += 1
    elif c <= -3: score -= 2
    elif c <= -1: score -= 1
    vc = signals["vol_ratio_5_20"]
    if vc >= 1.5 and c > 0:  score += 1
    elif vc >= 1.5 and c < 0: score -= 1
    if score >= 3:  return "🟢 看好"
    if score >= -1: return "🟡 中性"
    return "🔴 看衰"


# 全域節流 — 每次呼叫 _fetch_fundamentals 之間至少間隔這麼多秒
# 避免一次性吃光 Anthropic Tier 1 的 RPM / TPM 配額(50K input tokens/min)
# 在並行模式下這個 gap 變成「兩個 thread 之間的最小間隔」,實際整體節奏由
# ThreadPoolExecutor(max_workers) 控制。
_FETCH_FUNDAMENTALS_GAP_SEC = 3   # 兩次呼叫間最小間隔(秒)
_FETCH_PARALLEL_WORKERS = 2        # Tier 2 token 夠用 → 2 檔並行(撞 429 仍會耐心重試)
_last_fundamentals_call_ts: float = 0.0
_fundamentals_session_cache: dict[str, dict] = {}   # sym → result,跨 organize 共用


# 抽出到系統提示 → Anthropic 會幫我們 cache,第 2 個 stock 之後便宜 90%。
# ★2026-06-04 改版:拿掉 web_search。改成「使用者訊息會直接帶官方免費事實」,
#   Haiku 只負責把數字翻成白話 + 評分 → 純文字呼叫,成本趨近於零。
_FUNDAMENTALS_SYSTEM_PROMPT = """你是台股基本面整理助手。使用者會給你某一檔股票的**官方免費事實**
(估值/配息來自證交所、月營收來自證交所、新聞標題來自 Google News)。
你的工作是把這些事實翻成**白話描述 + 評分**。**只能用使用者給的事實,不要自己編造數字、不要查網路。**

**只回 JSON**,前後不要任何文字、不要 code fence、不要 ``` 包起來。

JSON 格式(每個欄位都要):
{
  "about": "這家公司在幹嘛,極度白話一句話(像跟完全不懂的人介紹),例如「幫全世界做晶片的代工廠」",
  "estimate": "估值白話描述",
  "estimate_score": int,
  "dividend": "配息白話描述",
  "dividend_score": int,
  "revenue": "營收動能白話描述",
  "revenue_score": int,
  "news": "近期新聞重點(一句話)",
  "news_score": int,
  "health_summary": "用一句白話總結這家公司的體質(綜合賺不賺錢、生意有沒有成長、有沒有配息、貴不貴),不要數字、像跟朋友講,例如「會賺錢、生意有成長,只是現在價位偏貴」"
}

評分標準(全部 int,依使用者給的數字判斷):
- estimate: 本益比<15→2 / 15-20→1 / 20-25→0 / 25-30→-1 / >30→-2(沒有本益比就看股價淨值比:<1.5→1 / 1.5-3→0 / >3→-1)
- dividend: 殖利率<1%→-1 / 1-3%→0 / 3-5%→1 / >5%→2
- revenue: 年增率<-10→-2 / -10~0→-1 / 0~10→0 / 10~25→1 / >25→2
- news: 重大利空→-2 / 利空→-1 / 中性→0 / 利多→1 / 重大利多→2

⚠️ 描述文字要**超白話、像跟朋友聊天**,不要財經術語、不要一堆數字。讓完全不懂股票的人也秒懂。
例如:
- estimate 不要寫「本益比 28 倍偏高」→ 寫「現在這價位算有點貴」
- dividend 不要寫「殖利率 4.2%」→ 寫「有發股息,還算大方」
- revenue 不要寫「月增 12% 年增 -5%」→ 寫「生意比上個月好,但比去年差一點」
- news → 看標題抓重點,寫「最近接到大訂單」這種一句話
每句 15~30 字、口語。**某一項使用者標示「查無」就寫「資料不足」+ 該 score 設 0。**"""


# Haiku 4.5 估價(美金/token,粗估,實際以帳單為準)
_PRICE_IN = 1.0 / 1_000_000
_PRICE_OUT = 5.0 / 1_000_000
_PRICE_CACHE_READ = 0.1 / 1_000_000
_PRICE_WEB_SEARCH = 0.01    # 每次搜尋


def _estimate_call_cost(resp) -> float:
    """從 resp.usage 粗估這一筆 API call 花了多少美金(含 web_search 次數)。"""
    try:
        u = resp.usage
        c = 0.0
        c += (getattr(u, "input_tokens", 0) or 0) * _PRICE_IN
        c += (getattr(u, "output_tokens", 0) or 0) * _PRICE_OUT
        c += (getattr(u, "cache_read_input_tokens", 0) or 0) * _PRICE_CACHE_READ
        c += (getattr(u, "cache_creation_input_tokens", 0) or 0) * _PRICE_IN
        stu = getattr(u, "server_tool_use", None)
        if stu is not None:
            c += (getattr(stu, "web_search_requests", 0) or 0) * _PRICE_WEB_SEARCH
        return c
    except Exception:
        return 0.0


def _fetch_fundamentals(sym: str, name: str) -> dict:
    """抓公司基本面 + 計算分數。★2026-06-04 改版:
    先用 free_fetch 免費抓官方事實(估值/配息/月營收/新聞標題),
    再做一次「純文字 Haiku」呼叫翻成白話 + 評分(不再用付費 web_search)。
    institutional(法人籌碼)改由官方外資大戶燈單獨處理,這裡固定資料不足。
    沒設 ANTHROPIC_API_KEY 或失敗時回 ok=False。
    內建節流避免 429,並支援同 session 快取(同一檔不重複查)。"""
    global _last_fundamentals_call_ts

    # ⚠️ 這個 helper 一定要先定義 — 不然錯誤路徑(429 等)會 UnboundLocalError 整個崩潰
    def _store_and_return(result: dict) -> dict:
        _fundamentals_session_cache[sym] = result
        return result

    # Session 快取:同一檔在同一次 deep analysis 內如果已查過,直接回傳
    if sym in _fundamentals_session_cache:
        return _fundamentals_session_cache[sym]

    # 節流(對並行也有效,因為這是 module-level 全域變數,有 GIL 保護)
    elapsed = time.time() - _last_fundamentals_call_ts
    if elapsed < _FETCH_FUNDAMENTALS_GAP_SEC:
        time.sleep(_FETCH_FUNDAMENTALS_GAP_SEC - elapsed)
    _last_fundamentals_call_ts = time.time()

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return _store_and_return({"ok": False, "error": "ANTHROPIC_API_KEY 未設"})
    try:
        from anthropic import Anthropic
    except ImportError:
        return _store_and_return({"ok": False, "error": "anthropic SDK 未安裝"})

    client = Anthropic()
    # 用 `or default` 而不是 getenv default,這樣空字串也會 fallback
    # (GitHub Actions 沒設 secret 時會把 env var 注成 "")
    model = (os.getenv("ANTHROPIC_MODEL") or "").strip() or "claude-haiku-4-5-20251001"

    # ★2026-06-04:先免費抓官方事實(估值/配息/月營收/新聞標題),取代付費 web_search。
    try:
        facts = free_fetch.gather_free_facts(sym, name)
    except Exception as e:
        print(f"⚠️ free_fetch 失敗 sym={sym}: {type(e).__name__}: {e}", flush=True)
        facts = {"facts_text": "", "data_date": "", "has_any": False}
    free_data_date = facts.get("data_date") or ""

    # 三項全部查無 → 不用浪費 Haiku 呼叫,直接回資料不足
    if not facts.get("has_any"):
        return _store_and_return({
            "ok": True,
            "estimate": "資料不足", "dividend": "資料不足", "revenue": "資料不足",
            "institutional": "資料不足", "news": "資料不足",
            "data_date": free_data_date,
            "estimate_score": 0, "dividend_score": 0, "revenue_score": 0,
            "institutional_score": 0, "news_score": 0,
            "cost_usd": 0.0,
        })

    # 🚀 Token 優化:長指令搬到 system prompt + cache_control(後面每檔便宜 90%)。
    # 純文字事實塊塞進 user message — 不再呼叫 web_search。
    user_msg = (f"請整理台股 {sym} {name}。以下是官方免費事實,只能根據這些判斷:\n\n"
                + facts.get("facts_text", "")).strip()

    # 主呼叫 + 429 重試:每分鐘 token 配額(50K)爆掉時,等 60 秒讓配額重置再試。
    # 配額是「每分鐘」,所以 sleep 要夠長(60s),且多試幾次才有意義。
    resp = None
    last_err = None
    for attempt in range(4):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=2000,
                system=[{
                    "type": "text",
                    "text": _FUNDAMENTALS_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                # ★2026-06-04:拿掉 web_search(最貴的部分)。純文字呼叫,事實已在 user_msg 裡。
                messages=[{"role": "user", "content": user_msg}],
            )
            break
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            is_rate = ("429" in err_str or "rate_limit" in err_str
                       or "rate limit" in err_str)
            if is_rate and attempt < 3:
                wait = 60 * (attempt + 1)   # 60s, 120s, 180s — 等每分鐘配額重置
                print(f"⏳ sym={sym} 撞 429,等 {wait}s 後重試"
                      f"(第 {attempt + 1}/3 次)…", flush=True)
                time.sleep(wait)
                continue
            # 非 rate limit 的錯誤,或重試用完 → 放棄這檔(印 log,但不讓整批崩潰)
            print(f"⚠️ _fetch_fundamentals API 錯誤 sym={sym}: "
                  f"{type(e).__name__}: {e}", flush=True)
            return _store_and_return({
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
            })
    if resp is None:
        print(f"⚠️ _fetch_fundamentals 重試後仍失敗 sym={sym}: {last_err}",
              flush=True)
        return _store_and_return({
            "ok": False,
            "error": f"重試後仍失敗: {last_err}",
        })

    try:
        # 🛡️ Anthropic web_search 模式下會有多個 text block(每次 search 前後都有
        # 思考文字),我們只要**最後一個** text block — 那才是 LLM 真正的 JSON 輸出
        text_blocks = [b.text for b in resp.content
                       if hasattr(b, "text") and b.text]
        text = (text_blocks[-1] if text_blocks else "").strip()

        # 🛡️ 三層 JSON 萃取(防 Haiku 亂回):
        # 1) 直接 parse
        # 2) Strip markdown code fence
        # 3) 抓最外層 { ... } 子字串
        data = None
        for attempt_text in _iter_json_candidates(text):
            try:
                data = json.loads(attempt_text)
                break
            except json.JSONDecodeError:
                continue

        if data is None:
            # 把原始回應印出來方便 debug(Actions log 看得到)
            print(f"⚠️ _fetch_fundamentals JSON 解析失敗,sym={sym}",
                  f"raw_text 前 300 字:{text[:300]!r}", flush=True)
            return _store_and_return({
                "ok": False,
                "error": f"JSON 解析失敗:{text[:100]!r}",
            })

        # data_date 用「免費資料的真實來源日期」(證交所那天),比 Haiku 自報的可靠。
        # institutional(法人籌碼)已改由官方外資「大戶燈」單獨處理,這裡固定資料不足/0。
        # 上櫃股目前沒有免費官方月營收 → 把「資料不足」換成誠實的「上櫃,營收待補」。
        _revenue = str(data.get("revenue", "資料不足"))
        if "資料不足" in _revenue:
            try:
                if free_fetch.get_market(sym) == "上櫃":
                    _revenue = "上櫃,營收待補"
            except Exception:
                pass
        return _store_and_return({
            "ok":                  True,
            "about":               str(data.get("about", "")).strip(),
            "health_summary":      str(data.get("health_summary", "")).strip(),
            "estimate":            str(data.get("estimate", "資料不足")),
            "dividend":            str(data.get("dividend", "資料不足")),
            "revenue":             _revenue,
            "institutional":       "資料不足",
            "news":                str(data.get("news", "資料不足")),
            "data_date":           (free_data_date or str(data.get("data_date", "")).strip())[:10],
            "estimate_score":      _safe_int(data.get("estimate_score")),
            "dividend_score":      _safe_int(data.get("dividend_score")),
            "revenue_score":       _safe_int(data.get("revenue_score")),
            "institutional_score": 0,
            "news_score":          _safe_int(data.get("news_score")),
            "cost_usd":            _estimate_call_cost(resp),
        })
    except Exception as e:
        print(f"⚠️ _fetch_fundamentals 例外 sym={sym}: {type(e).__name__}: {e}",
              flush=True)
        return _store_and_return({"ok": False, "error": f"{type(e).__name__}: {e}"})


def _iter_json_candidates(text: str):
    """產生 JSON 解析候選字串。"""
    # 1) 原文直接 try
    yield text
    # 2) Strip markdown code fence ```json ... ``` 或 ``` ... ```
    stripped = text
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        # 第一行 ```json 或 ```,丟掉
        lines = lines[1:]
        # 最後一行 ``` 也丟掉
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        yield "\n".join(lines).strip()
    # 3) 抓最外層 { ... } 子字串
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        yield text[start:end + 1]


def _safe_int(v) -> int:
    """容錯把任何東西轉成 int — None / 空字串 / 浮點 / 帶 "+" 號的字串都吞。"""
    if v is None:
        return 0
    try:
        return int(float(str(v).strip().replace("+", "")))
    except (ValueError, TypeError):
        return 0


# 公司體質 5 等級(好→差)。用估值/配息/營收三項好壞加總分級。
_HEALTH_LEVELS = [(3, "💎", "頂尖"), (1, "💪", "強健"),
                  (-1, "🆗", "普通"), (-3, "⚠️", "偏弱"), (-99, "🆘", "危險")]


def _company_health(fd: dict, sym: str) -> str:
    """回一行「公司體質」字串:符號 + 等級 + 白話。給「關於這檔」最上面顯示。
    ETF / 資料不足有專屬處理,永不從不足的資料硬評。"""
    if sym.startswith("00") and len(sym) >= 4:
        return "🧺 ETF　一籃子股票,沒有單一公司體質,主要看殖利率/折溢價"
    if not fd.get("ok"):
        return "📋 資料不足　查不到公司資料,先看技術面三盞燈"
    missing = sum(1 for k in ("estimate", "dividend", "revenue")
                  if ("資料不足" in str(fd.get(k, "")) or "待補" in str(fd.get(k, ""))))
    if missing >= 2:
        return "📋 資料不足　查不到足夠的估值/營收,先看技術面三盞燈"
    score = (_safe_int(fd.get("estimate_score")) + _safe_int(fd.get("dividend_score"))
             + _safe_int(fd.get("revenue_score")))
    symbol, name = "🆘", "危險"
    for thr, sym_i, name_i in _HEALTH_LEVELS:
        if score >= thr:
            symbol, name = sym_i, name_i
            break
    summary = str(fd.get("health_summary", "")).strip()
    if not summary:                       # 後備:用估值/配息/營收白話拼一句
        parts = [str(fd.get(k, "")).strip() for k in ("estimate", "dividend", "revenue")
                 if fd.get(k) and "資料不足" not in str(fd.get(k, ""))]
        summary = "、".join(parts[:3])
    return f"{symbol} 體質{name}　{summary}".strip()


_COMPUTE_PARALLEL_WORKERS = 3   # 技術分析 Fugle K 線抓取並行數 (改低點避免 Fugle 429)


def _prefetch_signals_parallel(syms: list[str]) -> dict[str, dict]:
    """並行抓 K 線 + 算技術訊號 — 用 ThreadPoolExecutor 同時開 6 個。
    回傳 {sym: signals_dict}。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out: dict[str, dict] = {}
    if not syms:
        return out
    with ThreadPoolExecutor(max_workers=_COMPUTE_PARALLEL_WORKERS) as pool:
        futures = {pool.submit(_compute_short_signals, s): s for s in syms}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                out[sym] = fut.result()
            except Exception as e:
                out[sym] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return out


def _prefetch_fundamentals_parallel(items: list[tuple[str, str]]) -> None:
    """並行抓 fundamentals — items 是 [(sym, name), ...]。
    結果直接寫進 _fundamentals_session_cache。同 sym 只抓一次。
    用 ThreadPoolExecutor 開 _FETCH_PARALLEL_WORKERS 個工作緒同時跑。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    todo: list[tuple[str, str]] = []
    seen: set[str] = set()
    for sym, name in items:
        if not sym or sym in seen or sym in _fundamentals_session_cache:
            continue
        seen.add(sym)
        todo.append((sym, name))
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=_FETCH_PARALLEL_WORKERS) as pool:
        futures = [pool.submit(_fetch_fundamentals, s, n) for s, n in todo]
        for _ in as_completed(futures):
            pass   # 結果已經寫進 cache,不需要在這裡蒐集


def _company_light_v2(fund: dict) -> str:
    """公司面燈號 — 估值 + 配息 + 營收。
    3 項裡有 2 項以上「資料不足」→ 給 ⚪ 資料不足(不裝中性)。"""
    if not fund.get("ok"):
        return "⚪ 資料不足"
    missing = sum(1 for k in ("estimate", "dividend", "revenue")
                  if "資料不足" in str(fund.get(k, "")))
    if missing >= 2:
        return "⚪ 資料不足"
    score = fund["estimate_score"] + fund["dividend_score"] + fund["revenue_score"]
    if score >= 3:  return "🟢 強"
    if score >= -1: return "🟡 平淡"
    return "🔴 弱"


def _chips_light_v2(fund: dict) -> str:
    """籌碼面燈號 — 法人籌碼 + 新聞。
    2 項都「資料不足」→ 給 ⚪ 資料不足(不裝中性)。"""
    if not fund.get("ok"):
        return "⚪ 資料不足"
    missing = sum(1 for k in ("institutional", "news")
                  if "資料不足" in str(fund.get(k, "")))
    if missing >= 2:
        return "⚪ 資料不足"
    score = fund["institutional_score"] + fund["news_score"]
    if score >= 2:  return "🟢 強"
    if score >= -1: return "🟡 平淡"
    return "🔴 弱"


def _combined_advice(short_light: str, super_short_light: str,
                     chips_light: str, company_light: str,
                     is_position: bool) -> str:
    """根據 4 個燈號給綜合建議文字。沒有基本面 (—) 時,只用技術版本。"""
    def _e(light: str) -> str:
        if "⚪" in light or "資料不足" in light: return "⚪"  # 資料不足,跟中性區分
        if "🟢" in light: return "🟢"
        if "🟡" in light: return "🟡"
        if "🔴" in light: return "🔴"
        return "—"
    s, ss, ch, co = _e(short_light), _e(super_short_light), _e(chips_light), _e(company_light)

    # ⚪ 跟 — 都當作「沒有可用資訊」
    _no_tech = s in ("—", "⚪") and ss in ("—", "⚪")
    _no_fund = ch in ("—", "⚪") and co in ("—", "⚪")

    # 技術 + 基本面「都」沒資料 → 直接講清楚,不要裝中性
    if _no_tech and _no_fund:
        return "⚪ 資料不足、無法判斷 — 建議自己查或稍後重跑"

    # 沒有基本面(只跑技術 / 基本面資料不足)→ 簡化版
    if _no_fund:
        if s == "🟢" and ss == "🟢":
            return "🟢 短期看好(基本面待補,請說「整體深度分析」)"
        if s == "🔴" and ss == "🔴":
            return "🔴 短期看衰(基本面待補)"
        if s == "🟡" and ss == "🟡":
            return "🟡 技術中性、再等等"
        if s == "🟢" and ss == "🔴":
            return "🟡 中期看好但超短線轉弱、觀察 1-2 天"
        if s == "🔴" and ss == "🟢":
            return "🟡 中期看衰但超短線在反彈、觀察是否站穩"
        return "🟡 技術訊號混合、再等等"

    # 有基本面 → 4 燈號矩陣
    if s == "🟢" and ss == "🟢":
        tech_key = "🟢"
    elif s == "🔴" and ss == "🔴":
        tech_key = "🔴"
    elif s == "🟢" and ss == "🔴":
        tech_key = "↘"
    elif s == "🔴" and ss == "🟢":
        tech_key = "↗"
    else:
        tech_key = "🟡"

    score_map = {"🟢": 1, "🟡": 0, "🔴": -1, "—": 0, "⚪": 0}
    f_score = score_map[ch] + score_map[co]
    if f_score >= 1:    fund_key = "🟢"
    elif f_score <= -1: fund_key = "🔴"
    else:               fund_key = "🟡"

    if is_position:
        matrix = {
            ("🟢", "🟢"): "🟢 短長線都好,可放心抱、可加碼",
            ("🟢", "🟡"): "🟢 短線看好、別貪太久(無長線 backup)",
            ("🟢", "🔴"): "🟡 小心追,可能是反彈陷阱",
            ("🟡", "🟢"): "🟡 等技術轉好、長線基本面 OK",
            ("🟡", "🟡"): "🟡 再等等、沒明顯訊號",
            ("🟡", "🔴"): "🔴 沒理由抱、考慮先獲利了結",
            ("🔴", "🟢"): "🟡 短線弱但長線好、觀察止跌",
            ("🔴", "🟡"): "🔴 沒理由抱、考慮停利停損",
            ("🔴", "🔴"): "🔴 盡快出場、雙弱沒戲",
            ("↘", "🟢"): "🟡 中期好但短線轉弱、先觀察 1-2 天",
            ("↘", "🟡"): "🟡 中期好但短線轉弱、可考慮先停利一部分",
            ("↘", "🔴"): "🔴 中期好但短線+基本面轉弱、考慮減碼",
            ("↗", "🟢"): "🟡 中期差但短線反彈+長線好、可觀察",
            ("↗", "🟡"): "🟡 中期差但短線反彈、空間有限",
            ("↗", "🔴"): "🔴 中期差又無長線支撐、反彈別追",
        }
    else:
        matrix = {
            ("🟢", "🟢"): "🟢 短長線都看好、可考慮進場",
            ("🟢", "🟡"): "🟢 短線可進、別久抱",
            ("🟢", "🔴"): "🟡 小心、可能是反彈陷阱",
            ("🟡", "🟢"): "🟡 等技術轉好再進、長線 OK",
            ("🟡", "🟡"): "🟡 再等等、沒明顯訊號",
            ("🟡", "🔴"): "🔴 不用進、不值得",
            ("🔴", "🟢"): "🟡 短線弱、若止跌可低接",
            ("🔴", "🟡"): "🔴 短線弱、不用進",
            ("🔴", "🔴"): "🔴 雙弱、不用進",
            ("↘", "🟢"): "🟡 中期看好但短線轉弱、等止穩",
            ("↘", "🟡"): "🟡 中期看好但短線弱、觀察",
            ("↘", "🔴"): "🔴 雙弱、不用進",
            ("↗", "🟢"): "🟡 短線反彈+長線好、觀察站穩",
            ("↗", "🟡"): "🟡 短線反彈但中期弱、小心追",
            ("↗", "🔴"): "🔴 反彈無支撐、不要追",
        }
    return matrix.get((tech_key, fund_key), "🟡 訊號不明、再等等")


def _light_emoji(light: str) -> str:
    """把任何燈號字串收斂成單一 emoji:⚪(資料不足)/🟢/🟡/🔴。
    動能燈是 5 段:🔥(強勢)當🟢、🟠(偏弱)當🔴,讓決策仍走 3 段邏輯。"""
    if "⚪" in light or "資料不足" in light:
        return "⚪"
    if "🔥" in light or "🟢" in light:
        return "🟢"
    if "🟠" in light or "🔴" in light:
        return "🔴"
    if "🟡" in light:
        return "🟡"
    return "⚪"   # 空白也當資料不足


def _tech_tier(short_light: str, super_short_light: str = "") -> str:
    """技術面 = 動能燈(短線燈號)本身。動能燈已經是 🟢/🟡/🔴/⚪,直接用。
    (超短線已不納入判斷,只看動能,避免被 2 天雜訊洗來洗去)"""
    return _light_emoji(short_light)


def _fund_tier(chips_light: str, company_light: str) -> str:
    """基本面整體 = 籌碼 + 公司:都🟢→🟢、都🔴→🔴、都⚪→⚪、其他→🟡。"""
    c, co = _light_emoji(chips_light), _light_emoji(company_light)
    if c == "⚪" and co == "⚪":
        return "⚪"
    if c == "🟢" and co == "🟢":
        return "🟢"
    if c == "🔴" and co == "🔴":
        return "🔴"
    return "🟡"


_MOM_ZH = {"🔥": "很強", "🟢": "偏多", "🟡": "中性", "🟠": "偏弱", "🔴": "很弱", "⚪": "沒資料"}


def _mom_level(light: str) -> str:
    """動能燈的原始 5 段:🔥/🟢/🟡/🟠/🔴,沒資料→⚪。"""
    s = str(light or "")
    if "⚪" in s or "資料不足" in s or not s.strip():
        return "⚪"
    for e in ("🔥", "🟢", "🟡", "🟠", "🔴"):
        if e in s:
            return e
    return "⚪"


def _watch_action(mom: str, f: str) -> tuple[str, str]:
    """沒持有(追蹤清單)的動作:5 段動能 × 基本面 → (動作, 白話原因)。
    動作詞:可以買 / 先買一點 / 再等等 / 先別碰。"""
    if mom == "🔴":
        return ("先別碰", "走勢明顯轉弱,等止跌再說")
    if mom == "🟠":
        return ("先別碰", "開始走弱了,先別追,等它站穩再看")
    if mom == "🟡":
        if f == "🟢":
            return ("再等等", "公司面不錯,等動能轉強再進場")
        if f == "🔴":
            return ("先別碰", "沒動能、公司體質又差,不值得進")
        return ("再等等", "還沒有明顯方向,先等等看")
    if mom == "🟢":
        if f == "🔴":
            return ("先別碰", "雖然在漲,但公司體質差,不碰")
        return ("先買一點", "穩穩在漲、基本面也撐得住,先進一些、不用搶")
    if mom == "🔥":
        if f == "🔴":
            return ("先別碰", "衝得兇但公司體質差,再強也別追")
        return ("可以買", "動能很強、正在噴,要買就要快,但別追太高")
    return ("再等等", "訊號不明,先等等")


def _held_action(mom: str, f: str) -> tuple[str, str]:
    """持有中的動作(摘要用,不含損益/到價細節)→ (動作, 白話原因)。"""
    if mom == "🔥":
        if f == "🔴":
            return ("續抱別加", "衝得兇但公司體質差,抱著別追加,到價分批出")
        return ("抱緊加碼", "動能很強、還在噴,抱緊讓它跑,基本面也行可考慮加碼")
    if mom == "🟢":
        if f == "🔴":
            return ("續抱但別貪", "還在漲但公司體質差,到價就分批出別凹")
        return ("續抱", "穩穩在漲,先抱著別賣太早")
    if mom == "🟡":
        if f == "🔴":
            return ("偏減碼", "沒明顯動能、公司體質又差,可分批先出一些")
        return ("抱著等", "沒明顯動能,先耐心抱著看,別急")
    if mom == "🟠":
        return ("先減碼", "開始轉弱、還沒破底,先收一些、看緊一點")
    return ("趕快賣", "走勢明顯轉弱,別凹,該走")


def _what_to_do(short_light: str, super_short_light: str,
                chips_light: str, company_light: str,
                is_position: bool) -> str:
    """根據燈號給「我該做啥」。動能用 5 段(🔥🟢🟡🟠🔴)、基本面用 3 段,
    讓強勢/偏多、弱勢/偏弱 各有不同講法。
    持有的最終定稿在 _position_advice(含損益/到價);這裡給追蹤 + 摘要用。"""
    mom = _mom_level(short_light)
    f = _fund_tier(chips_light, company_light)
    if mom == "⚪" and f == "⚪":
        return "⚪ 資料不足、先別動[技術跟基本面都還沒分析,先跑分析或自己查]"
    if f == "⚪":
        return (f"⏳ 等基本面[動能{_MOM_ZH[mom]},但基本面還沒分析,"
                f"先跑「基本面」再決定買賣]")
    if mom == "⚪":
        _lean = {"🟢": "偏多", "🟡": "中性", "🔴": "偏空"}
        return (f"⏳ 等技術[基本面{_lean[f]},但技術還沒分析,"
                f"先跑「技術面」再決定買賣]")
    # 過熱:強勢但漲多了 → 別追高(基本面爛仍然不要買)
    if mom == "🔥" and "過熱" in str(short_light or "") and f != "🔴":
        if is_position:
            return "抱著就好[漲很多了,抱著就好、先別追加,等拉回再考慮加碼]"
        return "再等等[漲太多了,追高風險高,想買等拉回再進]"
    action, reason = (_held_action if is_position else _watch_action)(mom, f)
    return f"{action}[{reason}]"


def _parse_price(v) -> float | None:
    s = str(v or "").replace(",", "").replace("$", "").replace("%", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _reached_target_pct(row: dict) -> int:
    """現價碰到的最高獲利目標(20/15/10/5),沒到任何一個回 0。"""
    cur = _parse_price(row.get("現價") or row.get("current_price"))
    if cur is None:
        return 0
    for pct, col in ((20, "淨賺20%價"), (15, "淨賺15%價"),
                     (10, "淨賺10%價"), (5, "淨賺5%價")):
        p = _parse_price(row.get(col))
        if p and cur >= p:
            return pct
    return 0


def _position_advice(row: dict) -> str:
    """持有股票的「我該做啥」:動能(5段)主導、基本面修正、損益決定講法。
      🔥 強勢 → 抱緊,可加碼;基本面爛 → 續抱別加
      🟢 偏多 → 續抱;基本面爛 → 續抱但別貪
      🟡 中性 → 抱著等;基本面爛 → 偏減碼
      🟠 偏弱 → 先減碼/留意(還沒破底);有賺先收一些
      🔴 弱勢 → 賣(有賺=獲利了結、虧=停損);基本面也爛 → 更堅決
    """
    short = row.get("短線燈號", "")
    chips = row.get("籌碼面燈號", "")
    comp = row.get("公司面燈號", "")
    mom = _mom_level(short)
    f = _fund_tier(chips, comp)
    if mom == "⚪":
        return "⏳ 等技術[技術還沒分析,先跑技術面再決定]"

    reached = _reached_target_pct(row)
    pnl = _parse_price(row.get("損益%"))
    if reached >= 5:
        gain = f"已賺 +{reached}%"
    elif pnl is not None and pnl > 0:
        gain = f"賺 {pnl:.0f}%"
    else:
        gain = ""
    head = f"{gain}、" if gain else ""

    if mom == "🔥":       # 強勢
        if "過熱" in str(short or ""):
            return f"抱著就好[{head}漲很多了,別追高,抱著、等拉回再說]"
        if f == "🔴":
            return f"抱著就好[{head}衝得兇但公司體質差,抱著就好、別追加]"
        return f"還能再買一點[{head}還在強勢往上,可以再買一筆讓它跑]"
    if mom == "🟢":       # 偏多
        if f == "🔴":
            return f"抱著就好[{head}還在漲但公司體質差,先抱著、別貪]"
        return f"抱著就好[{head}穩穩在漲,先抱著別賣太早]"
    if mom == "🟡":       # 中性
        if f == "🔴":
            return "賣1/3[沒動能、公司體質又差,先收 1/3 減壓(之後有機會再買回)]"
        return "抱著就好[沒明顯動能,先耐心抱著看,別急]"
    if mom == "🟠":       # 偏弱 → 有賺賣一半落袋、沒賺先抱著留意
        extra = "、基本面也差" if f == "🔴" else ""
        if gain:
            return f"賣一半[{gain}、開始轉弱{extra},先賣一半落袋(之後有機會再買回)]"
        return f"抱著就好[開始轉弱{extra}、還沒破底,先留意,破了就走]"
    # 🔴 弱勢 → 全部賣掉
    extra = "、基本面也差更該走" if f == "🔴" else ""
    if gain:
        return f"全部賣掉[{gain}又明顯轉弱{extra},獲利了結出場]"
    return f"全部賣掉[明顯轉弱又在虧{extra},別凹,停損出場]"


def resync_and_fill_names(scope: str = "all") -> dict:
    """重算交易 + 補空白名稱,**依 scope 只動該動的分頁**:
      - positions:manual_sync 重建部位/損益/目標賣價 + 補 部位/實際損益/股票交易 名稱
                   (完全不碰追蹤清單)
      - watchlist:只補 追蹤清單 名稱(不 manual_sync、不碰部位)
      - all:兩邊都做
    """
    do_pos = scope in ("all", "positions")
    do_wl = scope in ("all", "watchlist")
    name_cache: dict[str, str] = {}
    n_wl = n_pos = n_realized = n_trades = 0

    # 1) 補追蹤清單名稱(只有 watchlist / all 才做)
    if do_wl:
        try:
            for w in (sheets.load_watchlist() or []):
                if w.get("_error"):
                    continue
                sym = str(w.get("symbol") or w.get("代號") or "").strip()
                cur = str(w.get("name") or w.get("名稱") or "").strip()
                if not sym or cur:
                    continue
                nm = name_cache.get(sym) or _lookup_stock_name(sym)
                if not nm:
                    continue
                name_cache[sym] = nm
                if sheets_writer.upsert_watchlist_item(
                        symbol=sym, 代號=sym, name=nm, 名稱=nm).get("ok"):
                    n_wl += 1
        except Exception as e:
            print(f"⚠️ 補追蹤清單名稱失敗: {e}", flush=True)

    # 只跑追蹤清單 → 照追蹤理由排序後結束,完全不碰部位
    if not do_pos:
        try:
            sheets_writer.sort_watchlist()
        except Exception as e:
            print(f"⚠️ 追蹤清單排序失敗: {e}", flush=True)
        return {"ok": True, "watchlist": n_wl, "positions": 0,
                "realized": 0, "trades": 0}

    # 2) manual_sync — 重建部位 / 損益 / 實際損益 / 目標賣價公式
    sync = sheets_writer.manual_sync(timeout=120)
    if not sync.get("ok"):
        return {"ok": False, "error": f"manual_sync 失敗: {sync.get('error')}"}

    # 3) 補部位名稱
    try:
        for p in (sheets.load_positions() or []):
            if p.get("_error"):
                continue
            sym = str(p.get("symbol") or "").strip()
            cur = str(p.get("name") or "").strip()
            if not sym or cur:
                continue
            nm = name_cache.get(sym) or _lookup_stock_name(sym)
            if not nm:
                continue
            name_cache[sym] = nm
            if sheets_writer.upsert_position(
                    symbol=sym, 代號=sym, name=nm, 名稱=nm).get("ok"):
                n_pos += 1
    except Exception as e:
        print(f"⚠️ 補部位名稱失敗: {e}", flush=True)

    # 4) 直接補實際損益名稱(獨立,不依賴部位 / 追蹤清單)
    try:
        need: dict[str, str] = {}
        for r in (sheets.fetch_tab("實際損益") or []):
            if r.get("_error"):
                continue
            sym = str(r.get("symbol") or r.get("代號") or "").strip()
            nm0 = str(r.get("name") or r.get("名稱") or "").strip()
            if sym and not nm0 and sym not in need:
                looked = name_cache.get(sym) or _lookup_stock_name(sym)
                if looked:
                    need[sym] = looked
        if need:
            wb = sheets_writer.backfill_realized_names(need)
            n_realized = wb.get("filled", 0) if wb.get("ok") else 0
    except Exception as e:
        print(f"⚠️ 補實際損益名稱失敗: {e}", flush=True)

    # 5) 補「股票交易」名稱(交易表本身名稱留空也幫補)
    n_trades = 0
    try:
        trades_tab = os.getenv("PORTFOLIO_TRADES_TAB", "股票交易")
        need_t: dict[str, str] = {}
        for r in (sheets.fetch_tab(trades_tab) or []):
            if r.get("_error"):
                continue
            sym = str(r.get("symbol") or r.get("代號") or "").strip()
            nm0 = str(r.get("name") or r.get("名稱") or "").strip()
            if sym and not nm0 and sym not in need_t:
                looked = name_cache.get(sym) or _lookup_stock_name(sym)
                if looked:
                    need_t[sym] = looked
        if need_t:
            wb = sheets_writer.backfill_trade_names(need_t)
            n_trades = wb.get("filled", 0) if wb.get("ok") else 0
    except Exception as e:
        print(f"⚠️ 補股票交易名稱失敗: {e}", flush=True)

    # 6) 順便整理追蹤清單 — 此時股票部位 / 實際損益 已重建,順序保證正確:
    #    移除已持有的、重複留第一個、把賣光過的補「曾經」。
    cleaned = {}
    try:
        cw = sheets_writer.cleanup_watchlist()
        if cw.get("ok"):
            cleaned = {"removedHeld": cw.get("removedHeld", 0),
                       "removedDup": cw.get("removedDup", 0),
                       "added曾經": cw.get("added", 0)}
            print(f"   🧹 追蹤清單整理:{cleaned}", flush=True)
    except Exception as e:
        print(f"⚠️ 整理追蹤清單失敗: {e}", flush=True)

    return {"ok": True, "watchlist": n_wl, "positions": n_pos,
            "realized": n_realized, "trades": n_trades, "cleaned": cleaned}


def _recompute_advice(scope: str = "all") -> dict:
    """【獨立最後一步】讀齊 Sheet 上 4 個燈號,重算「我該做啥」寫回。
    技術面 / 基本面分析各自只寫自己的燈號,這一步才把它們合成最終結論 —
    所以「我該做啥」永遠是「技術+基本面都到齊」之後才定稿。
    scope: 'all' / 'positions' / 'watchlist'。回傳寫了幾筆。"""
    do_pos = scope in ("all", "positions")
    do_wl = scope in ("all", "watchlist")
    n_pos = n_wl = 0

    if do_pos:
        # 讀完整 row(要現價 + 目標價 才能判斷「到價該不該賣」)
        tab = os.getenv(sheets.POSITIONS_TAB_ENV, sheets.DEFAULT_POSITIONS_TAB)
        payloads = []
        for r in (sheets.fetch_tab(tab) or []):
            if r.get("_error"):
                continue
            sym = str(r.get("代號") or r.get("symbol") or "").replace("'", "").strip()
            if not sym:
                continue
            advice = _position_advice(r)   # 到價+技術 的賣出邏輯
            payloads.append({"symbol": sym, "代號": sym,
                             "我該做啥": advice, "綜合建議": advice})
        ok = _bulk_or_parallel(tab, payloads, sheets_writer.upsert_position)
        n_pos = sum(1 for v in ok.values() if v)

    if do_wl:
        wtab = os.getenv(sheets.WATCHLIST_TAB_ENV, sheets.DEFAULT_WATCHLIST_TAB)
        payloads = []
        for sym, lg in _build_existing_lights_map("watchlist").items():
            advice = _what_to_do(lg.get("short", ""), lg.get("super_short", ""),
                                 lg.get("chips", ""), lg.get("company", ""),
                                 is_position=False)
            payloads.append({"symbol": sym, "代號": sym,
                             "我該做啥": advice, "綜合建議": advice})
        ok = _bulk_or_parallel(wtab, payloads, sheets_writer.upsert_watchlist_item)
        n_wl = sum(1 for v in ok.values() if v)

    return {"positions": n_pos, "watchlist": n_wl}


# ---------------------------------------------------------------------------
# 🔔 盤中變化提醒 — 跟「上次的燈號快照」比對,翻燈就記成「今天的變化」
# ---------------------------------------------------------------------------
_MOM_RANK = {"🔥": 2, "🟢": 1, "🟡": 0, "🟠": -1, "🔴": -2}


def _change_msg(old_e: str, new_e: str, is_pos: bool) -> tuple[str, str] | None:
    """比較舊→新動能等級,回 (方向, 白話訊息);沒有有意義的變化回 None。"""
    o, n = _MOM_RANK.get(old_e), _MOM_RANK.get(new_e)
    if o is None or n is None or o == n:
        return None
    if n > o:   # 變強 → 買方向
        if is_pos:
            return ("買", "越漲越有勁了,抱著的續抱,想加碼也行")
        return ("買", "開始漲起來了,想買的可以看一下")
    # 變弱 → 賣方向
    if is_pos:
        return ("賣", "開始軟掉了,手上有的要當心,可能要準備跑")
    return ("賣", "走勢轉弱了,想買的先別急")


def detect_intraday_changes(scope: str = "all") -> dict:
    """跟上次燈號快照比對,把翻燈的記成「今天的變化」(存隱藏分頁)。
    每次更新的最後跑一次。免費(純比對,不打 AI)。"""
    do_pos = scope in ("all", "positions", "positions_new")
    do_wl = scope in ("all", "watchlist", "watchlist_new")

    prev: dict[str, str] = {}
    try:
        r = sheets_writer.load_snapshot()
        if r.get("ok") and r.get("snapshot"):
            prev = json.loads(r["snapshot"]) or {}
    except Exception:
        prev = {}

    cur: dict[str, str] = {}
    changes: list[dict] = []
    now = _now_tw_str()

    def _scan(rows, is_pos):
        for row in (rows or []):
            if row.get("_error"):
                continue
            sym = str(row.get("代號") or row.get("symbol") or "").replace("'", "").strip()
            if not sym:
                continue
            name = str(row.get("名稱") or row.get("name") or "").strip()
            new_e = _mom_level(row.get("短線燈號") or "")
            cur[sym] = new_e
            old_e = prev.get(sym)
            if old_e and old_e != "⚪" and new_e != "⚪":
                res = _change_msg(old_e, new_e, is_pos)
                if res:
                    changes.append({"time": now, "scope": "持有" if is_pos else "追蹤",
                                    "symbol": sym, "name": name, "dir": res[0], "msg": res[1]})

    try:
        if do_pos:
            tab = os.getenv(sheets.POSITIONS_TAB_ENV, sheets.DEFAULT_POSITIONS_TAB)
            _scan(sheets.fetch_tab(tab), True)
        if do_wl:
            _scan(sheets.load_watchlist(), False)
    except Exception as e:
        print(f"⚠️ 偵測變化讀取失敗: {e}", flush=True)
        return {"ok": False, "error": str(e)}

    merged = dict(prev)
    merged.update(cur)   # 只更新這次掃到的 scope,另一邊的快照保留
    try:
        if changes:
            sheets_writer.log_changes(changes)
        sheets_writer.save_snapshot(json.dumps(merged, ensure_ascii=False))
    except Exception as e:
        print(f"⚠️ 寫入變化/快照失敗: {e}", flush=True)
    print(f"   🔔 偵測到 {len(changes)} 個變化", flush=True)
    return {"ok": True, "changes": len(changes)}


def _build_existing_lights_map(source: str) -> dict[str, dict]:
    """從 Sheet 讀現有的 4 個燈號 — 用於「只跑一邊時讓綜合建議仍能算對」。
    source = 'positions' 或 'watchlist'。"""
    try:
        if source == "positions":
            tab = os.getenv(sheets.POSITIONS_TAB_ENV, sheets.DEFAULT_POSITIONS_TAB)
            rows = sheets.fetch_tab(tab)
        else:
            rows = sheets.load_watchlist()
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for r in rows or []:
        if not r or r.get("_error"):
            continue
        sym = str(r.get("代號") or r.get("symbol") or "").replace("'", "").strip()
        if not sym:
            continue
        out[sym] = {
            "short":       str(r.get("短線燈號") or ""),
            "super_short": str(r.get("超短線燈號") or ""),
            "chips":       str(r.get("籌碼面燈號") or ""),
            "company":     str(r.get("公司面燈號") or ""),
            "stuck":       str(r.get("卡住天數") or ""),
        }
    return out


def _parallel_upsert(payloads: list[dict], writer, max_workers: int = 5) -> dict:
    """同時寫多筆回 Sheet(原本一筆一筆寫很慢)。回 {代號: 是否成功}。"""
    import concurrent.futures as _cf
    out: dict[str, bool] = {}
    if not payloads:
        return out

    def _w(pl):
        sym = str(pl.get("symbol") or pl.get("代號") or "")
        try:
            return sym, bool(writer(**pl).get("ok"))
        except Exception as e:
            print(f"⚠️ 平行寫回 {sym} 失敗: {e}", flush=True)
            return sym, False

    with _cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for sym, ok in ex.map(_w, payloads):
            out[sym] = ok
    return out


def _bulk_or_parallel(tab: str, payloads: list[dict], writer) -> dict:
    """最快:一次批次寫回整批(bulk_upsert)。Apps Script 沒這動作就自動退回逐筆平行。
    回 {代號: 是否成功}。"""
    if not payloads:
        return {}
    try:
        r = sheets_writer.bulk_upsert(tab, payloads)
        if r and r.get("ok"):
            return {str(p.get("symbol") or p.get("代號")): True for p in payloads}
        print(f"⚠️ 批次寫入未成功({r.get('error') if r else 'no resp'}),改用平行", flush=True)
    except Exception as e:
        print(f"⚠️ 批次寫入例外,改用平行: {e}", flush=True)
    return _parallel_upsert(payloads, writer)


def _organize_v2_positions(update_technical: bool, update_fundamentals: bool,
                            organized_at: str, fee_rate: float, fee_min: float,
                            signals_cache: dict[str, dict] | None = None,
                            symbols_filter: set | None = None) -> dict:
    """跑股票部位的整理 — 技術跟基本面**分開更新**。
    - update_technical=True:寫 現價/市值/損益/6 個技術描述/2 個技術燈號/技術整理時間
    - update_fundamentals=True:寫 5 個基本面描述/2 個基本面燈號/基本面整理時間
    - 綜合建議**永遠更新**(用本次新算 + 另一邊從 Sheet 讀的舊燈號)
    - symbols_filter:list/set,只處理指定代號(失敗重跑用)
    """
    positions = sheets.load_positions()
    if positions and positions[0].get("_error"):
        return {"error": positions[0]["_error"]}
    # 失敗重跑單檔用 — 只留 symbols_filter 裡的
    if symbols_filter:
        positions = [p for p in positions
                     if str(p.get("symbol", "")).strip() in symbols_filter]

    # 只跑一邊時,需要從 Sheet 讀另一邊的舊燈號才能算「綜合建議」
    existing_lights = (_build_existing_lights_map("positions")
                       if not (update_technical and update_fundamentals)
                       else {})

    # 若要更新技術且沒提供 cache,自己並行抓
    if update_technical and signals_cache is None:
        syms_to_compute = [
            str(p.get("symbol", "")).strip()
            for p in positions
            if str(p.get("symbol", "")).strip()
            and int(p.get("shares") or 0) > 0
            and float(p.get("total_cost") or 0) > 0
        ]
        signals_cache = _prefetch_signals_parallel(syms_to_compute)
    elif signals_cache is None:
        signals_cache = {}

    results = []
    to_write: list[dict] = []   # 先收集,最後平行寫回(加速)
    for p in positions:
        sym = str(p.get("symbol", "")).strip()
        shares = int(p.get("shares") or 0)
        total_cost = float(p.get("total_cost") or 0)
        # 分析按鈕**不補名稱** — 名稱由「📋 重算交易+補名稱」按鈕專門處理
        name = str(p.get("name") or "").strip()
        if shares <= 0 or total_cost <= 0:
            continue
        prev = existing_lights.get(sym, {})

        # === 技術部分:更新 or 從 Sheet 讀舊燈號 ===
        tech_payload: dict = {}
        if update_technical:
            signals = signals_cache.get(sym) or _compute_short_signals(sym)
            if not signals.get("ok"):
                # 寫失敗標記到 Sheet — 不要靜默跳過,讓使用者看到哪一檔卡哪
                err_short = str(signals.get("error", "未知錯誤"))[:200]
                print(f"⚠️ 技術分析失敗 sym={sym}: {err_short}", flush=True)
                fail_payload = {
                    "symbol": sym, "代號": sym,
                    "name":   name, "名稱": name,
                    "今天表現":     f"❌ Fugle 抓 K 線失敗:{err_short}",
                    "最新表現":     f"❌ Fugle 抓 K 線失敗:{err_short}",
                    "技術資料時間": "(失敗)",
                    "最近3天":      "—",
                    "這週氛圍":     "—",
                    "近10天走勢":   "—",
                    "量能變化":     "—",
                    "離20天高低":   "—",
                    "短線燈號":     "⚪ 資料不足",
                    "超短線燈號":   "⚪ 資料不足",
                    "技術整理時間": f"{organized_at} (失敗)",
                }
                to_write.append(fail_payload)
                results.append({"symbol": sym, "name": name,
                                 "error": signals.get("error")})
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
            short_light, _mom_reason = _momentum_light(signals)
            super_light = _super_short_term_light(signals)
            # 卡住天數:動能熄火(🟡 中性)連續幾天 → 滿 3 天觸發換股提醒
            try:
                _prev_stuck = int(float(prev.get("stuck") or 0))
            except Exception:
                _prev_stuck = 0
            _stuck = (_prev_stuck + 1) if _mom_level(short_light) == "🟡" else 0
            tech_payload = {
                "現價":         round(price, 2),
                "市值":         round(gross, 2),
                "損益":         round(pnl, 2),
                "損益%":        round(pnl_pct, 2),
                "今天表現":     signals["today_desc"],
                "最新表現":     signals["today_desc"],
                "技術資料時間": organized_at,   # 你上次更新的時間(到秒);K線是哪天的看「最新表現」
                "最近3天":      signals["last3d_desc"],
                "這週氛圍":     signals["weekly_mood_desc"],
                "近10天走勢":   signals["ma10_desc"],
                "量能變化":     signals["volume_desc"],
                "離20天高低":   signals["range20_desc"],
                "短線燈號":     short_light,
                "動能原因":     _mom_reason,
                "超短線燈號":   super_light,
                "相對強度":     _relative_strength(signals),
                "停損價":       _stop_price(signals),
                "卡住天數":     _stuck,
                "技術整理時間": organized_at,
            }
        else:
            # 不重算 → 從 Sheet 讀現有燈號
            short_light = prev.get("short", "")
            super_light = prev.get("super_short", "")
            pnl_pct = 0  # not used when only updating fundamentals

        # === 基本面部分:更新 or 從 Sheet 讀舊燈號 ===
        fund_payload: dict = {}
        if update_fundamentals:
            fd = _fetch_fundamentals(sym, name)
            if fd.get("ok"):
                chips_light = _chips_light_v2(fd)
                company_light = _company_light_v2(fd)
                fund_payload = {
                    "公司簡介":        fd.get("about", ""),
                    "公司體質":        _company_health(fd, sym),
                    "估值":           fd["estimate"],
                    "配息":           fd["dividend"],
                    "營收動能":        fd["revenue"],
                    "法人籌碼":        fd["institutional"],
                    "近期新聞重點":    fd["news"],
                    "籌碼面燈號":      chips_light,
                    "公司面燈號":      company_light,
                    "基本面整理時間":   organized_at,
                    "基本面資料時間":   organized_at,   # 你上次更新的時間(到秒)
                    "本次花費":         f"${fd.get('cost_usd', 0):.4f}",
                }
            else:
                err_short = str(fd.get("error", "未知錯誤"))[:300]
                chips_light = "⚪ 資料不足"
                company_light = "⚪ 資料不足"
                fund_payload = {
                    "估值":           f"❌ API 失敗:{err_short}",
                    "配息":           "—",
                    "營收動能":        "—",
                    "法人籌碼":        "—",
                    "近期新聞重點":    "—",
                    "籌碼面燈號":      "⚪ 資料不足",
                    "公司面燈號":      "⚪ 資料不足",
                    "基本面整理時間":   f"{organized_at} (失敗)",
                    "基本面資料時間":   "(失敗)",
                }
        else:
            chips_light = prev.get("chips", "")
            company_light = prev.get("company", "")

        # 「我該做啥」不在這裡寫 — 改由最後獨立的 _recompute_advice 讀齊 4 燈才算,
        # 確保它永遠是「技術+基本面都到齊」之後才定稿(本步只負責寫技術/基本面燈)。
        advice = _what_to_do(short_light, super_light, chips_light, company_light,
                              is_position=True)   # 只給本次回覆摘要用,不寫 Sheet
        payload = {
            "symbol": sym, "代號": sym,
            "name":   name, "名稱": name,
            **tech_payload,
            **fund_payload,
        }
        to_write.append(payload)
        results.append({
            "symbol":            sym,
            "name":              name,
            "short_light":       short_light,
            "super_short_light": super_light,
            "chips_light":       chips_light,
            "company_light":     company_light,
            "advice":            advice,
        })

    # 平行寫回 Sheet(同時寫多檔,比一筆一筆快很多)
    ok_map = _bulk_or_parallel(
        os.getenv(sheets.POSITIONS_TAB_ENV, sheets.DEFAULT_POSITIONS_TAB),
        to_write, sheets_writer.upsert_position)
    n_ok = sum(1 for v in ok_map.values() if v)
    n_fail = len(to_write) - n_ok
    for r in results:
        r["written"] = ok_map.get(str(r.get("symbol")), False)
    return {"n": len(results), "n_ok": n_ok, "n_fail": n_fail, "items": results}


def _organize_v2_watchlist(update_technical: bool, update_fundamentals: bool,
                            organized_at: str,
                            signals_cache: dict[str, dict] | None = None,
                            symbols_filter: set | None = None) -> dict:
    """跑追蹤清單的整理 — 技術跟基本面**分開更新**(對應 _organize_v2_positions)。
    symbols_filter:只處理指定代號(失敗重跑用)。"""
    rows = sheets.load_watchlist()
    if rows and rows[0].get("_error"):
        return {"error": rows[0]["_error"]}
    if symbols_filter:
        rows = [r for r in rows
                if str(r.get("symbol") or r.get("代號") or "").strip() in symbols_filter]

    existing_lights = (_build_existing_lights_map("watchlist")
                       if not (update_technical and update_fundamentals)
                       else {})

    if update_technical and signals_cache is None:
        syms = [
            str(r.get("symbol") or r.get("代號") or "").strip()
            for r in rows
            if str(r.get("symbol") or r.get("代號") or "").strip()
        ]
        signals_cache = _prefetch_signals_parallel(syms)
    elif signals_cache is None:
        signals_cache = {}

    results = []
    to_write: list[dict] = []   # 先收集,最後平行寫回(加速)
    for row in rows:
        sym = str(row.get("symbol") or row.get("代號") or "").strip()
        if not sym:
            continue
        # 分析按鈕**不補名稱** — 名稱由「📋 重算交易+補名稱」按鈕專門處理
        name = str(row.get("name") or row.get("名稱") or "").strip()
        prev = existing_lights.get(sym, {})

        # === 技術部分 ===
        tech_payload: dict = {}
        if update_technical:
            signals = signals_cache.get(sym) or _compute_short_signals(sym)
            if not signals.get("ok"):
                err_short = str(signals.get("error", "未知錯誤"))[:200]
                print(f"⚠️ 技術分析失敗 sym={sym}: {err_short}", flush=True)
                fail_payload = {
                    "symbol": sym, "代號": sym,
                    "name":   name, "名稱": name,
                    "今天表現":     f"❌ Fugle 抓 K 線失敗:{err_short}",
                    "最新表現":     f"❌ Fugle 抓 K 線失敗:{err_short}",
                    "技術資料時間": "(失敗)",
                    "最近3天":      "—",
                    "這週氛圍":     "—",
                    "近10天走勢":   "—",
                    "量能變化":     "—",
                    "離20天高低":   "—",
                    "短線燈號":     "⚪ 資料不足",
                    "超短線燈號":   "⚪ 資料不足",
                    "技術整理時間": f"{organized_at} (失敗)",
                }
                to_write.append(fail_payload)
                results.append({"symbol": sym, "name": name,
                                 "error": signals.get("error")})
                continue
            short_light, _mom_reason = _momentum_light(signals)
            super_light = _super_short_term_light(signals)
            tech_payload = {
                "現價":         signals["current_price"],
                "今天表現":     signals["today_desc"],
                "最新表現":     signals["today_desc"],
                "技術資料時間": organized_at,   # 你上次更新的時間(到秒);K線是哪天的看「最新表現」
                "最近3天":      signals["last3d_desc"],
                "這週氛圍":     signals["weekly_mood_desc"],
                "近10天走勢":   signals["ma10_desc"],
                "量能變化":     signals["volume_desc"],
                "離20天高低":   signals["range20_desc"],
                "短線燈號":     short_light,
                "動能原因":     _mom_reason,
                "超短線燈號":   super_light,
                "相對強度":     _relative_strength(signals),
                "停損價":       _stop_price(signals),
                "技術整理時間": organized_at,
            }
        else:
            short_light = prev.get("short", "")
            super_light = prev.get("super_short", "")

        # === 基本面部分 ===
        fund_payload: dict = {}
        if update_fundamentals:
            fd = _fetch_fundamentals(sym, name)
            if fd.get("ok"):
                chips_light = _chips_light_v2(fd)
                company_light = _company_light_v2(fd)
                fund_payload = {
                    "公司簡介":        fd.get("about", ""),
                    "公司體質":        _company_health(fd, sym),
                    "估值":           fd["estimate"],
                    "配息":           fd["dividend"],
                    "營收動能":        fd["revenue"],
                    "法人籌碼":        fd["institutional"],
                    "近期新聞重點":    fd["news"],
                    "籌碼面燈號":      chips_light,
                    "公司面燈號":      company_light,
                    "基本面整理時間":   organized_at,
                    "基本面資料時間":   organized_at,   # 你上次更新的時間(到秒)
                    "本次花費":         f"${fd.get('cost_usd', 0):.4f}",
                }
            else:
                err_short = str(fd.get("error", "未知錯誤"))[:300]
                chips_light = "⚪ 資料不足"
                company_light = "⚪ 資料不足"
                fund_payload = {
                    "估值":           f"❌ API 失敗:{err_short}",
                    "配息":           "—",
                    "營收動能":        "—",
                    "法人籌碼":        "—",
                    "近期新聞重點":    "—",
                    "籌碼面燈號":      "⚪ 資料不足",
                    "公司面燈號":      "⚪ 資料不足",
                    "基本面整理時間":   f"{organized_at} (失敗)",
                    "基本面資料時間":   "(失敗)",
                }
        else:
            chips_light = prev.get("chips", "")
            company_light = prev.get("company", "")

        # 「我該做啥」改由最後獨立的 _recompute_advice 算(讀齊 4 燈才定稿)
        advice = _what_to_do(short_light, super_light, chips_light, company_light,
                              is_position=False)   # 只給本次回覆摘要用,不寫 Sheet
        payload = {
            "symbol": sym, "代號": sym,
            "name":   name, "名稱": name,
            **tech_payload,
            **fund_payload,
        }
        to_write.append(payload)
        results.append({
            "symbol":            sym,
            "name":              name,
            "short_light":       short_light,
            "super_short_light": super_light,
            "chips_light":       chips_light,
            "company_light":     company_light,
            "advice":            advice,
        })

    # 平行寫回 Sheet(同時寫多檔,加速)
    ok_map = _bulk_or_parallel(
        os.getenv(sheets.WATCHLIST_TAB_ENV, sheets.DEFAULT_WATCHLIST_TAB),
        to_write, sheets_writer.upsert_watchlist_item)
    n_ok = sum(1 for v in ok_map.values() if v)
    n_fail = len(to_write) - n_ok
    for r in results:
        r["written"] = ok_map.get(str(r.get("symbol")), False)
    return {"n": len(results), "n_ok": n_ok, "n_fail": n_fail, "items": results}


@tool(
    "organize_all_technical",
    "**「整體技術分析」一鍵工具** — 對「股票部位」+「追蹤清單」每一檔抓 K 線、跑 6 個短線訊號、"
    "給 2 個技術燈號(短線 5-10 天 + 超短線 2-5 天)、寫綜合建議(技術版),全部寫回 Sheet。"
    "**使用者說「整體技術分析」「整理技術」「跑技術面」直接呼叫這個**(快,12 檔約 30-60 秒)。"
    "回覆**只**說「技術面整理好了」+ 簡短列看好/看衰的代號,不要長篇大論,不要 render 完整表格,"
    "完整資料使用者自己看 Sheet。"
    "**單檔重跑**:若使用者說「某檔失敗 / 沒抓到 / 幫我重跑 2330」,用 symbols=['2330'] 只跑那幾檔,"
    "省錢省時間,不要重跑全部。",
    {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["all", "positions", "watchlist"],
                "description": "範圍:all=全部(預設)、positions=只股票部位、watchlist=只追蹤清單",
            },
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "只跑這幾檔代號(失敗單檔重跑用,例如 ['2330','2454'])。給了就只跑這些,其他不動。",
            },
        },
        "required": [],
    },
)
async def organize_all_technical(args: dict) -> dict:
    """可選參數:
      - scope: 'all'(預設) / 'positions' / 'watchlist'
      - symbols: list[str],只跑指定代號(失敗單檔重跑用,例如 ['2330'])
    """
    organized_at = _now_tw_str()
    fee_rate = float(os.getenv("USER_FEE_RATE", "0.001425"))
    fee_min  = float(os.getenv("USER_FEE_MIN", "1"))
    scope = (args or {}).get("scope", "all")
    do_pos = scope in ("all", "positions")
    do_wl  = scope in ("all", "watchlist")
    symbols_filter = set(str(s).strip() for s in (args or {}).get("symbols", []) if s)

    # 不再自動 manual_sync — 重建部位/損益/補名稱交給「📋 重算交易+補名稱」按鈕專責,
    # 避免每次分析都把實際損益的名稱清掉重補(多餘且會打架)
    sync_res = None

    # 並行抓需要的 sym 的 K 線
    all_syms: set[str] = set()
    try:
        if do_pos:
            for p in (sheets.load_positions() or []):
                s = str(p.get("symbol", "")).strip()
                if s and not p.get("_error"):
                    all_syms.add(s)
        if do_wl:
            for w in (sheets.load_watchlist() or []):
                s = str(w.get("symbol") or w.get("代號") or "").strip()
                if s and not w.get("_error"):
                    all_syms.add(s)
    except Exception:
        pass
    signals_cache = _prefetch_signals_parallel(list(all_syms))

    pos = wl = None
    if do_pos:
        pos = _organize_v2_positions(update_technical=True, update_fundamentals=False,
                                      organized_at=organized_at,
                                      fee_rate=fee_rate, fee_min=fee_min,
                                      signals_cache=signals_cache,
                                      symbols_filter=symbols_filter or None)
    if do_wl:
        wl = _organize_v2_watchlist(update_technical=True, update_fundamentals=False,
                                     organized_at=organized_at,
                                     signals_cache=signals_cache,
                                     symbols_filter=symbols_filter or None)
    return _envelope({
        "ok":           True,
        "deep":         False,
        "scope":        scope,
        "organized_at": organized_at,
        "sync":         sync_res,
        "n_signals":    len(signals_cache),
        "positions":    pos,
        "watchlist":    wl,
    })


@tool(
    "organize_all_deep",
    "**「整體深度分析」一鍵工具** — 技術 + 基本面全套。除了技術面,對每檔還抓估值、"
    "配息、營收動能、近期新聞(改用證交所/Google News 官方免費資料 + 一次純文字 AI 翻白話),"
    "給公司面燈號、寫完整綜合建議。(法人籌碼改由官方外資大戶燈每日免費更新。)"
    "**使用者說「整體深度分析」「深度分析」「跑全部」直接呼叫這個**。"
    "**告訴使用者**這會跑一陣子,建議週末或晚上跑。"
    "回覆**只**說「深度分析整理好了」+ 簡短列看好/看衰的代號,不要長篇大論。"
    "**單檔重跑**:若使用者說「某檔失敗 / 沒抓到 / 幫我重跑 2330 的深度」,用 symbols=['2330'] 只跑那幾檔,"
    "深度分析很貴,千萬不要為了一兩檔失敗就重跑全部。",
    {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["all", "positions", "watchlist"],
                "description": "範圍:all=全部(預設)、positions=只股票部位、watchlist=只追蹤清單",
            },
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "只跑這幾檔代號(失敗單檔重跑用,例如 ['2330','2454'])。給了就只跑這些,其他不動。",
            },
        },
        "required": [],
    },
)
async def organize_all_deep(args: dict) -> dict:
    """可選參數:
      - scope: 'all'(預設) / 'positions' / 'watchlist'
      - symbols: list[str],只跑指定代號(失敗單檔重跑用)
    """
    organized_at = _now_tw_str()
    fee_rate = float(os.getenv("USER_FEE_RATE", "0.001425"))
    fee_min  = float(os.getenv("USER_FEE_MIN", "1"))
    scope = (args or {}).get("scope", "all")
    do_pos = scope in ("all", "positions")
    do_wl  = scope in ("all", "watchlist")
    symbols_filter = set(str(s).strip() for s in (args or {}).get("symbols", []) if s)

    # 不再自動 manual_sync — 重建部位/損益/補名稱交給「📋 重算交易+補名稱」按鈕專責,
    # 避免每次分析都把實際損益的名稱清掉重補(多餘且會打架)
    sync_res = None

    # 收集要 prefetch 基本面的 (sym, name) — 限定 scope + symbols_filter
    _fundamentals_session_cache.clear()
    items_to_prefetch: list[tuple[str, str]] = []
    try:
        if do_pos:
            for p in (sheets.load_positions() or []):
                sym = str(p.get("symbol", "")).strip()
                if sym and not (p.get("_error")):
                    if symbols_filter and sym not in symbols_filter:
                        continue
                    name = str(p.get("name") or "").strip() or _lookup_stock_name(sym)
                    items_to_prefetch.append((sym, name))
        if do_wl:
            for w in (sheets.load_watchlist() or []):
                sym = str(w.get("symbol") or w.get("代號") or "").strip()
                if sym and not (w.get("_error")):
                    if symbols_filter and sym not in symbols_filter:
                        continue
                    name = str(w.get("name") or w.get("名稱") or "").strip() or _lookup_stock_name(sym)
                    items_to_prefetch.append((sym, name))
    except Exception:
        pass
    _prefetch_fundamentals_parallel(items_to_prefetch)

    pos = wl = None
    if do_pos:
        pos = _organize_v2_positions(update_technical=False, update_fundamentals=True,
                                      organized_at=organized_at,
                                      fee_rate=fee_rate, fee_min=fee_min,
                                      signals_cache=None,
                                      symbols_filter=symbols_filter or None)
    if do_wl:
        wl = _organize_v2_watchlist(update_technical=False, update_fundamentals=True,
                                     organized_at=organized_at,
                                     signals_cache=None,
                                     symbols_filter=symbols_filter or None)
    return _envelope({
        "ok":              True,
        "deep":            True,
        "scope":           scope,
        "organized_at":    organized_at,
        "sync":            sync_res,
        "n_prefetched":    len(items_to_prefetch),
        "positions":       pos,
        "watchlist":       wl,
    })


@tool(
    "what_to_do_now",
    "**「此刻要做什麼」按鈕專用** — 不重新分析,直接讀 Sheet 上現有的 v2 燈號 + 綜合建議,"
    "整理出**今天該注意/該動手**的清單。看的欄位:短線燈號、超短線燈號、籌碼面燈號、公司面燈號、綜合建議。"
    "回傳會分成 3 區:🟢 看好的部位/追蹤、🔴 看衰的部位、🟢 追蹤清單可進場。"
    "**使用者按按鈕、或說「我現在該做什麼」「現在該動哪些」直接呼叫**。",
    {"type": "object", "properties": {}, "required": []},
)
async def what_to_do_now(args: dict) -> dict:
    """直接讀 Sheet 的燈號欄位,不打 Fugle、不打 Anthropic。秒回。"""
    out = {
        "positions_buy_signal":  [],  # 部位 + 短線 🟢
        "positions_sell_signal": [],  # 部位 + 短線 🔴 或 綜合建議含「出場/停損」
        "watchlist_buy_signal":  [],  # 追蹤 + 短線 🟢
        "watchlist_avoid":       [],  # 追蹤 + 短線 🔴
        "deep_analysis_at":      None,
    }

    def _light(v: str | None) -> str:
        s = str(v or "")
        if "🔥" in s or "🟢" in s: return "🟢"
        if "🟠" in s or "🔴" in s: return "🔴"
        if "🟡" in s: return "🟡"
        return "—"

    try:
        for p in (sheets.load_positions() or []):
            if p.get("_error"):
                continue
            sym = str(p.get("symbol", "")).strip()
            if not sym:
                continue
            short = _light(p.get("短線燈號") or p.get("short_light"))
            advice = str(p.get("我該做啥") or p.get("綜合建議") or "").strip()
            name = str(p.get("name") or p.get("名稱") or "").strip()
            entry = {"symbol": sym, "name": name, "advice": advice,
                     "pnl_pct": p.get("損益%") or p.get("pnl_pct")}
            if short == "🟢":
                out["positions_buy_signal"].append(entry)
            if short == "🔴" or any(k in advice for k in ("出場", "停損", "減碼", "停利")):
                out["positions_sell_signal"].append(entry)

        for w in (sheets.load_watchlist() or []):
            if w.get("_error"):
                continue
            sym = str(w.get("symbol") or w.get("代號") or "").strip()
            if not sym:
                continue
            short = _light(w.get("短線燈號"))
            advice = str(w.get("我該做啥") or w.get("綜合建議") or "").strip()
            name = str(w.get("name") or w.get("名稱") or "").strip()
            entry = {"symbol": sym, "name": name, "advice": advice}
            if short == "🟢":
                out["watchlist_buy_signal"].append(entry)
            elif short == "🔴":
                out["watchlist_avoid"].append(entry)
            # 取最新的「基本面整理時間」當深度分析時間
            t = str(w.get("基本面整理時間") or "").strip()
            if t and (out["deep_analysis_at"] is None or t > out["deep_analysis_at"]):
                out["deep_analysis_at"] = t
    except Exception as e:
        return _envelope({"error": f"{type(e).__name__}: {e}"})

    return _envelope(out)


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
    organize_all_technical,
    organize_all_deep,
    what_to_do_now,
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
