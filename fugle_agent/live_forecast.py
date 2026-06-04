# ⬆️【要上傳 2026-06-04 21:56】live_forecast.py — 即時組裝 + 上市/上櫃標示
"""即時組裝層 —— 打開頁面當下,把每檔的「此刻最新數字」抓齊,餵給 forecasts 引擎。

分工:
  - 慢的(趨勢/RSI/今天漲跌/量)→ 重用既有、已上線的 tools._compute_short_signals。
  - 盤中微結構(近 N 分方向、買賣力道、VWAP、加速)→ intraday_lights 那幾個 helper。
  - 外資 → foreign_flow(全市場一次抓、共用);隔夜美股 + 大盤 → 一次抓、共用。
  - 現價 / 損益 → 即時算(不存 Sheet)。

全程防呆:任何一步失敗就讓那個數字 = None,引擎會自己降級(少投幾票 → 信心低 / 說不準),
絕不讓整頁壞掉。實際 Fugle 連線無法在開發機驗,需部署後確認。
"""

from __future__ import annotations

import datetime
from typing import Any, Optional

from . import forecasts
from . import intraday_lights as il


def _sign(x: Any) -> Optional[int]:
    n = il._f(x)
    if n is None:
        return None
    return 1 if n > 0 else (-1 if n < 0 else 0)


def _market_label(quote: dict) -> str:
    """從 Fugle 報價的市場別欄位判斷 上市/上櫃/興櫃。判不出回 ""。"""
    m = str((quote or {}).get("market") or (quote or {}).get("exchange") or "").upper().strip()
    if m in ("TSE", "TWSE", "TWS", "LISTED", "TW"):
        return "上市"
    if m in ("OTC", "TPEX", "OTCEX", "ROTC", "TWO"):
        return "上櫃"
    if m in ("ESB", "EMERGING", "EMERGINGSTOCK", "ROTC2"):
        return "興櫃"
    return ""


def _now_tw_date() -> datetime.date:
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()


# ───────────────────────── 全市場共用快照 ─────────────────────────

def market_snapshot() -> dict:
    """大盤方向 + 隔夜美股 + 外資(全市場一次抓)。所有股票共用,只抓一次。"""
    out: dict[str, Any] = {"mkt_today": None, "mkt_swing": None,
                           "headwind": False, "us_overnight_pct": None,
                           "foreign": {}}
    # 大盤
    try:
        from . import market_context
        ctx = market_context.get_market_context()
        out["headwind"] = bool(ctx.get("is_headwind"))
        s = market_context._twii_signals()
        if s:
            out["mkt_today"] = _sign(s.get("change_pct"))
            out["mkt_swing"] = 1 if s.get("above_ma10") else -1
    except Exception:
        pass
    # 隔夜美股(費半 + 標普 平均)
    try:
        from . import us_market
        vals = []
        for sym in ("^SOX", "^GSPC"):
            q = us_market.quote(sym)
            if isinstance(q, dict) and not q.get("error") and q.get("changePercent") is not None:
                vals.append(float(q["changePercent"]))
        if vals:
            out["us_overnight_pct"] = sum(vals) / len(vals)
    except Exception:
        pass
    # 外資:抓最近一個有資料的交易日(全市場一包)
    try:
        from . import foreign_flow
        for back in range(0, 5):
            day = _now_tw_date() - datetime.timedelta(days=back)
            fmap = foreign_flow._fetch_twse(day)
            if fmap:
                out["foreign"] = fmap
                break
    except Exception:
        pass
    return out


# ───────────────────────── 單檔即時預測 ─────────────────────────

def _compute_pnl(sym: str, last_price: Optional[float], shares: int,
                 total_cost: float, fee_rate: float, fee_min: float) -> Optional[dict]:
    """即時算現值損益(已扣手續費 + 證交稅)。資料不足回 None。"""
    if last_price is None or shares <= 0 or total_cost <= 0:
        return None
    is_etf = sym.startswith("00") and len(sym) >= 4
    tax_rate = 0.001 if is_etf else 0.003
    gross = last_price * shares
    fee = max(fee_min, gross * fee_rate)
    tax = gross * tax_rate
    net = gross - fee - tax
    pnl = net - total_cost
    return {
        "現價": round(last_price, 2),
        "損益": round(pnl, 2),
        "損益%": round(pnl / total_cost * 100, 2) if total_cost else 0,
    }


def forecast_for(sym: str, *, shares: int = 0, total_cost: float = 0.0,
                 is_holding: bool = False, msnap: dict | None = None,
                 fee_rate: float = 0.001425, fee_min: float = 1.0) -> dict:
    """單檔:抓齊此刻最新數字 → 三盞預測 + 怎麼辦 + 現價/損益。"""
    msnap = msnap or {}
    sym = str(sym).strip()

    # 1) 日線/今天訊號(重用既有、已上線測過的 _compute_short_signals)
    sig: dict = {"ok": False}
    try:
        from .tools import _compute_short_signals
        sig = _compute_short_signals(sym) or {"ok": False}
    except Exception:
        sig = {"ok": False}

    # 2) 即時報價 + 盤中分鐘K + 逐筆
    quote: dict = {}
    candles: dict = {}
    ticks: dict = {}
    last_price = high = low = None
    try:
        from .client import FugleClient
        c = FugleClient()
        quote = c.quote(sym) or {}
        last_price = (il._f(quote.get("lastPrice")) or il._f(quote.get("closePrice"))
                      or il._f(quote.get("price")))
        high = il._f(quote.get("highPrice")) or il._f(quote.get("high"))
        low = il._f(quote.get("lowPrice")) or il._f(quote.get("low"))
        candles = c.intraday_candles(sym) or {}
        series = il.series_from_candles(candles)
        ticks = c.intraday_ticks(sym, limit=200) or {}
        if len(series) < 2:
            series = il.series_from_ticks(ticks)
    except Exception:
        series = []

    if last_price is None and sig.get("ok"):
        last_price = il._f(sig.get("current_price"))

    now_move, accel = il.now_move_and_accel(series)
    close_strength = None
    if high is not None and low is not None and high > low and last_price is not None:
        close_strength = max(0.0, min(1.0, (last_price - low) / (high - low)))

    def _sg(key):
        return sig.get(key) if sig.get("ok") else None

    snap = {
        # 🔮 下一小時
        "now_move_pct": now_move,
        "pressure":     il.pressure_from_ticks(ticks),
        "vwap_pct":     il.vwap_pct_from_candles(candles, last_price),
        "vol_ratio":    _sg("today_vol_ratio"),
        "accel":        accel,
        "mkt_now":      msnap.get("mkt_today"),
        # 🌤️ 明天
        "close_strength":   close_strength,
        "today_change_pct": _sg("today_change_pct") if _sg("today_change_pct") is not None
                            else il._f(quote.get("changePercent")),
        "today_vol_ratio":  _sg("today_vol_ratio"),
        "us_overnight_pct": msnap.get("us_overnight_pct"),
        "mkt_today":        msnap.get("mkt_today"),
        # 📅 三天後
        "dist_ma_pct":     _sg("dist_ma10_pct"),
        "rsi":             _sg("rsi7"),
        "foreign_dir":     _sign((msnap.get("foreign") or {}).get(sym)),
        "vol_ratio_5_20":  _sg("vol_ratio_5_20"),
        "mkt_swing":       msnap.get("mkt_swing"),
    }

    res = forecasts.all_three(snap, is_holding=is_holding,
                              headwind=bool(msnap.get("headwind")))
    res["pnl"] = _compute_pnl(sym, last_price, shares, total_cost, fee_rate, fee_min)
    res["price"] = round(last_price, 2) if last_price is not None else None
    res["market"] = _market_label(quote)
    try:
        from . import market_context
        res["data_time"] = market_context._fugle_data_time(quote)
    except Exception:
        res["data_time"] = ""
    res["ok"] = (last_price is not None) or bool(sig.get("ok"))
    return res


def prefetch(items: list[tuple[str, int, float]], *, is_holding: bool,
             max_workers: int = 6) -> dict[str, dict]:
    """並行算一整頁。items = [(sym, shares, total_cost), ...]。回 {sym: result}。"""
    from concurrent.futures import ThreadPoolExecutor
    out: dict[str, dict] = {}
    syms = [(s, sh, tc) for s, sh, tc in items if s]
    if not syms:
        return out
    msnap = market_snapshot()

    def _one(t):
        s, sh, tc = t
        try:
            return s, forecast_for(s, shares=sh, total_cost=tc,
                                   is_holding=is_holding, msnap=msnap)
        except Exception:
            return s, {"ok": False, "next_hour": {"lean": "⚪ 資料不足", "conf": "低", "reason": ""},
                       "tomorrow": {"lean": "⚪ 資料不足", "conf": "低", "reason": ""},
                       "three_day": {"lean": "⚪ 資料不足", "conf": "低", "reason": ""},
                       "action": "⚪ 資料不足", "pnl": None, "price": None, "data_time": ""}

    try:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(syms))) as pool:
            for s, r in pool.map(_one, syms):
                out[s] = r
    except Exception:
        pass
    return out
