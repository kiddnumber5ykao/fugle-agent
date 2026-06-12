# 🚀【最新 2026-06-12 · DEPLOY-0612】pro_signals.py — 專業指標→白話「該不該出手」判斷
"""把盤中分鐘K + 逐筆,算成業界標準指標(VWAP / MACD / RSI / CVD內外盤累積 / RVOL相對量),
再收斂成「一句白話結論」:🟢可以考慮出手 / 🟡先別急 / ⚪不明朗。
通知只給白話結論,不秀數字(raw 留著想回測再用)。

⚠️ 技術分析是機率不是真理:這套是「提高勝率的綜合判讀」,不是保證,最終由使用者決定。
"""
from __future__ import annotations

from typing import Optional

from . import intraday_lights as il
from .indicators import ema, rsi


def _closes_vols(candles) -> tuple[list[float], list[float]]:
    rows = (candles or {}).get("data") if isinstance(candles, dict) else None
    closes, vols = [], []
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        c = il._f(r.get("close")) or il._f(r.get("price"))
        v = (il._f(r.get("volume")) or il._f(r.get("size"))
             or il._f(r.get("tradeVolume")) or 0.0)
        if c is not None:
            closes.append(c)
            vols.append(v or 0.0)
    return closes, vols


def vwap_above(candles, price) -> Optional[bool]:
    """現價是否站上 VWAP(成交量加權均價)。算不出回 None。"""
    pct = il.vwap_pct_from_candles(candles, price)
    return None if pct is None else (pct >= 0)


def macd_hist(closes) -> tuple[Optional[float], Optional[float]]:
    """回 (最新 MACD 柱, 近3根柱的變化)。柱>0 偏多;變化>0 = 動能轉強。資料不足回 (None,None)。"""
    if len(closes) < 35:
        return (None, None)
    e12, e26 = ema(closes, 12), ema(closes, 26)
    macd_line = [(a - b) if (a is not None and b is not None) else None
                 for a, b in zip(e12, e26)]
    mvals = [m for m in macd_line if m is not None]
    if len(mvals) < 10:
        return (None, None)
    sig = ema(mvals, 9)
    hist = [(m - s) for m, s in zip(mvals, sig) if s is not None]
    if not hist:
        return (None, None)
    chg = (hist[-1] - hist[-4]) if len(hist) >= 4 else None
    return (hist[-1], chg)


def rsi_now(closes, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    r = rsi(closes, period)
    vals = [x for x in r if x is not None]
    return vals[-1] if vals else None


def cvd_recent(ticks, window_min: float = 10) -> Optional[float]:
    """近 window 分鐘累積量差(主動買量 − 主動賣量)。>0 買盤、<0 賣盤。沒資料回 None。"""
    rows = (ticks or {}).get("data") if isinstance(ticks, dict) else None
    if not rows:
        return None
    timed = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = il._to_epoch_sec(r.get("time") or r.get("date") or r.get("timestamp"))
        if t is not None:
            timed.append((t, r))
    if not timed:
        return None
    last_t = max(t for t, _ in timed)
    cutoff = last_t - window_min * 60
    buy = sell = 0.0
    for t, r in timed:
        if t < cutoff:
            continue
        tt = r.get("tickType")
        sz = il._f(r.get("size")) or il._f(r.get("volume")) or 1.0
        if tt in (1, "1"):
            buy += sz
        elif tt in (2, "2"):
            sell += sz
    return buy - sell


def rvol(candles) -> Optional[float]:
    """相對量:最近 3 根分鐘量 vs 今天到現在每分鐘平均量。>1 放量、<1 量縮。資料不足回 None。"""
    _, vols = _closes_vols(candles)
    vols = [v for v in vols if v > 0]
    if len(vols) < 6:
        return None
    avg = sum(vols) / len(vols)
    recent = sum(vols[-3:]) / 3
    return (recent / avg) if avg > 0 else None


def entry_verdict(price: float, candles, ticks) -> dict:
    """綜合判斷「現在是不是好出手點」。回 {emoji, headline, reasons[], raw{}}。"""
    closes, _ = _closes_vols(candles)
    above_vwap = vwap_above(candles, price)
    mh, mh_chg = macd_hist(closes)
    cvd = cvd_recent(ticks, 10)
    rv = rvol(candles)
    r_now = rsi_now(closes)

    score = 0
    reasons: list[str] = []

    # VWAP:站上 = 加分(更強);在下 = 不扣分(逢低反彈初期本來就在均價下,別否決)
    if above_vwap is True:
        score += 1
        reasons.append("站上均價(買方占上風)")
    elif above_vwap is False:
        reasons.append("還在均價下(反彈初期,正常)")

    if cvd is not None:
        if cvd > 0:
            score += 1
            reasons.append("有人在買(主動買多)")
        elif cvd < 0:
            score -= 1
            reasons.append("還在賣(主動賣多)")

    if mh_chg is not None:
        _eps = max(abs(price) * 1e-5, 1e-6)   # 防浮點雜訊被當成轉向
        if mh_chg > _eps:
            score += 1
            reasons.append("動能在轉強")
        elif mh_chg < -_eps:
            score -= 1
            reasons.append("動能在轉弱")

    if r_now is not None:
        if r_now <= 32:
            reasons.append("跌深(容易反彈)")
        elif r_now >= 72:
            score -= 1
            reasons.append("過熱(容易回)")

    vol_ok = rv is not None and rv >= 1.3
    if rv is not None:
        reasons.append("量夠" if vol_ok else "量偏小")

    if score >= 2 and vol_ok:
        emoji, headline = "🟢", "看起來不錯的出手點"
    elif score <= -1:
        emoji, headline = "🟡", "先別急(時機未到)"
    else:
        emoji, headline = "⚪", "氣氛不明朗,自己斟酌"

    return {"emoji": emoji, "headline": headline, "reasons": reasons,
            "raw": {"above_vwap": above_vwap, "macd_hist": mh, "macd_chg": mh_chg,
                    "cvd": cvd, "rvol": rv, "rsi": r_now}}
