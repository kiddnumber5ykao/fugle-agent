# 🔖最新批次 4L-0608-1454 ｜ ⬆️【要上傳】live_forecast.py — snapshot 補 60 分窗 + 日線(60min/day slope inputs)
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

# 「下一小時」第 1~4 點統一用的時間窗:最近 N 分鐘。
#   • 第1點 走勢 = 最近 N 分鐘漲跌%
#   • 第3點 剛轉向 = 最近 N 分鐘 vs 前 N 分鐘(所以 turn_and_accel 吃 2N 的總窗)
#   • 第4點 買賣力道 = 最近 N 分鐘的逐筆內外盤
NH_RECENT_MIN = 10


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
    """大盤方向 + 隔夜美股 + 外資(全市場一次抓)。所有股票共用,只抓一次。
    三塊互相獨立 → 並行抓(尤其美股 yfinance 慢),整頁載入更快。"""
    out: dict[str, Any] = {"mkt_today": None, "mkt_swing": None,
                           "headwind": False, "us_overnight_pct": None,
                           "foreign": {}}

    def _twii():
        # 大盤:只算一次 _twii_signals,順便導出順逆風(不再額外呼叫 get_market_context,省一輪)
        try:
            from . import market_context
            s = market_context._twii_signals()
            if s:
                chg = s.get("change_pct") or 0
                out["mkt_today"] = _sign(chg)
                out["mkt_swing"] = 1 if s.get("above_ma10") else -1
                out["headwind"] = (not s.get("above_ma10")) and chg < 0
        except Exception:
            pass

    def _us():
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

    def _foreign():
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

    from concurrent.futures import ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(lambda f: f(), (_twii, _us, _foreign)))
    except Exception:
        _twii(); _us(); _foreign()
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

    # 1) 即時報價 + 盤中分鐘K + 逐筆(先抓報價,等下日線訊號共用同一份 → 省一次報價)
    #    逐筆 → 算「買賣力道(內外盤)」,是下一小時的正式一票。
    quote: dict = {}
    candles: dict = {}
    ticks: dict = {}
    last_price = high = low = None
    series: list = []
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
        # 多抓逐筆,確保「最近 10 / 30 / 60 分鐘」在熱門股也涵蓋得到(之後在 pressure 裡用時間篩)
        ticks = c.intraday_ticks(sym, limit=2000) or {}
        if len(series) < 2:
            series = il.series_from_ticks(ticks)
    except Exception:
        series = []

    # 2) 日線/今天訊號(把上面已抓的報價傳進去,不再重抓一次報價)
    sig: dict = {"ok": False}
    try:
        from .tools import _compute_short_signals
        sig = _compute_short_signals(sym, quote=(quote or None)) or {"ok": False}
    except Exception:
        sig = {"ok": False}

    if last_price is None and sig.get("ok"):
        last_price = il._f(sig.get("current_price"))

    # 下一小時的方向 = 抓「剛開始要往上/往下走」:把近20分切前後半,用後半(最近約10分)
    # 的方向當主軸,再加一個『剛轉向』判斷。不是跟一開盤比、也不是看整段淨變化。
    now_move, prior_move, turn, accel = il.turn_and_accel(series, window_min=NH_RECENT_MIN * 2)
    # 30 / 60 分窗(同算法、較長尺度):前N分 / 後N分
    now_move_30, prior_move_30, _t30, _a30 = il.turn_and_accel(series, window_min=60)
    now_move_60, prior_move_60, _t60, _a60 = il.turn_and_accel(series, window_min=120)
    close_strength = None
    if high is not None and low is not None and high > low and last_price is not None:
        close_strength = max(0.0, min(1.0, (last_price - low) / (high - low)))

    def _sg(key):
        return sig.get(key) if sig.get("ok") else None

    snap = {
        # ⚡ 下一小時(預測下 N 分斜率 = 2×後 − 前,再乘買賣力道權重)
        "now_move_pct":   now_move,     # 後10分%(=現在速度;today_close 也用)
        "prior_move_pct": prior_move,   # 前10分%(外推用)
        "pressure_net":   il.pressure_net_from_ticks(ticks, window_min=NH_RECENT_MIN),  # 內外盤連續淨值 -1~1
        # ⏱️ 下30分鐘(30 分窗)
        "now_move_pct_30":   now_move_30,
        "prior_move_pct_30": prior_move_30,
        "pressure_net_30":   il.pressure_net_from_ticks(ticks, window_min=30),
        # 🕐 下60分鐘(60 分窗)
        "now_move_pct_60":   now_move_60,
        "prior_move_pct_60": prior_move_60,
        "pressure_net_60":   il.pressure_net_from_ticks(ticks, window_min=60),
        # 📅 一天(日線斜率;權重用『外資』= 下面三天後那段的 foreign_dir)
        "now_move_pct_day":   _sg("day_recent_pct"),
        "prior_move_pct_day": _sg("day_prior_pct"),
        # 下面這幾個舊鍵保留(today_close / 防呆用,不影響新算法)
        "turn":         turn,
        "pressure":     il.pressure_from_ticks(ticks, window_min=NH_RECENT_MIN),
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

    pnl = _compute_pnl(sym, last_price, shares, total_cost, fee_rate, fee_min)
    res = forecasts.all_three(snap, is_holding=is_holding,
                              headwind=bool(msnap.get("headwind")),
                              pnl_pct=(pnl or {}).get("損益%"))
    res["pnl"] = pnl
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
             max_workers: int = 12) -> dict[str, dict]:
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
