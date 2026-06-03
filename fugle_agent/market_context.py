# 📅 ★最新版★ 上傳於 2026-06-02 19:45  大盤改 Fugle 優先抓加權(IX0001)+ Yahoo 退路重試
"""大盤順逆風 — 全頁共用背景,不分個股,不存 Sheet(當下算當下用)。

時間邏輯:
  盤前(台股還沒開,台北 09:00 前)→ 看昨晚美股(標普/那斯達克)+ 費半,給開盤氣氛。
  開盤後(09:00 起)→ 只看加權指數自己(站上 10 日線 + 今天漲跌);
                     美股退場(開盤價已消化隔夜美股,不重複算)。

只影響「買」:逆風(is_headwind=True)時,買進類動作會被踩煞車;賣/停損不受影響。

資料來源:yfinance(免費)。^TWII 加權指數、^GSPC 標普、^IXIC 那斯達克、^SOX 費半。
"""
from __future__ import annotations

import datetime

from . import us_market

_TWII = "^TWII"
_US = {"標普": "^GSPC", "那斯達克": "^IXIC", "費半": "^SOX"}


def _now_tw() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


_FUGLE_TAIEX = "IX0001"   # 發行量加權股價指數(加權)在 Fugle 的代號


def _index_bars() -> list[dict]:
    """回排序好的加權指數日 K 線。Fugle 優先(穩),抓不到退 Yahoo(含重試)。"""
    # 1) Fugle(跟你的個股同源,比較穩)
    try:
        from fugle_agent.client import FugleClient
        r = FugleClient().candles(_FUGLE_TAIEX)
        bars = sorted([b for b in ((r or {}).get("data") or [])
                       if b.get("close") is not None],
                      key=lambda b: str(b.get("date", "")))
        if len(bars) >= 2:
            return bars
    except Exception:
        pass
    # 2) Yahoo 退路(end 開區間 → to_date 給「明天」今天那根才進得來);抓不到重試 2 次
    tomorrow = (_now_tw().date() + datetime.timedelta(days=1)).isoformat()
    for _ in range(2):
        try:
            r = us_market.candles(_TWII, to_date=tomorrow)
            bars = sorted([b for b in ((r or {}).get("data") or [])
                           if b.get("close") is not None],
                          key=lambda b: str(b.get("date", "")))
            if len(bars) >= 2:
                return bars
        except Exception:
            pass
    return []


def _twii_signals() -> dict | None:
    """回 {close, ma10, change_pct, above_ma10, date} 或 None(抓不到)。"""
    bars = _index_bars()
    if len(bars) < 2:
        return None
    closes = [float(b["close"]) for b in bars]
    last = closes[-1]
    prev = closes[-2]
    ma10 = sum(closes[-10:]) / min(len(closes), 10)
    return {
        "close": last,
        "ma10": ma10,
        "change_pct": (last / prev - 1) * 100 if prev else 0.0,
        "above_ma10": last >= ma10,
        "date": str(bars[-1].get("date", ""))[:10],
    }


def _us_overnight() -> dict:
    """回 {名稱: 漲跌%},抓不到的略過。"""
    out: dict[str, float] = {}
    for zh, sym in _US.items():
        q = us_market.quote(sym)
        if isinstance(q, dict) and q.get("changePercent") is not None and not q.get("error"):
            out[zh] = float(q["changePercent"])
    return out


def get_market_context() -> dict:
    """回大盤順逆風結果(給橫幅 + 買進踩煞車用)。"""
    now = _now_tw()
    updated = now.strftime("%Y-%m-%d %H:%M:%S")
    # 台北 09:00 前算盤前(用昨晚美股);09:00 起算盤中/盤後(用加權指數)
    is_premarket = now.hour < 9

    if is_premarket:
        us = _us_overnight()
        if not us:
            return {"light": "🟡 普通", "phase": "盤前", "is_headwind": False,
                    "reason": "抓不到美股資料,當作普通", "updated": updated}
        avg = sum(us.values()) / len(us)
        parts = "、".join(f"{k} {v:+.1f}%" for k, v in us.items())
        if avg >= 0.5:
            light, head = "🟢 偏強", False
            reason = f"昨晚美股偏強({parts}),今天開盤氣氛偏好"
        elif avg <= -0.5:
            light, head = "🔴 偏弱", True
            reason = f"昨晚美股偏弱({parts}),今天開盤氣氛偏差,買進保守點"
        else:
            light, head = "🟡 普通", False
            reason = f"昨晚美股漲跌互見({parts}),開盤氣氛普通"
        return {"light": light, "phase": "盤前", "is_headwind": head,
                "reason": reason, "updated": updated}

    # 開盤後:只看加權指數
    s = _twii_signals()
    if not s:
        return {"light": "🟡 普通", "phase": "盤中", "is_headwind": False,
                "reason": "抓不到加權指數,當作普通", "updated": updated}
    chg = s["change_pct"]
    dtag = f"(資料 {s.get('date', '')})" if s.get("date") else ""
    if s["above_ma10"] and chg >= 0:
        light, head = "🟢 順風", False
        reason = f"加權站上 10 日線、{chg:+.1f}%,大盤偏多,順風 {dtag}"
    elif (not s["above_ma10"]) and chg < 0:
        light, head = "🔴 逆風", True
        reason = f"加權跌破 10 日線、{chg:+.1f}%,大盤偏弱,買進保守點 {dtag}"
    else:
        light, head = "🟡 普通", False
        reason = f"加權在均線附近、{chg:+.1f}%,方向不明 {dtag}"
    return {"light": light, "phase": "盤中", "is_headwind": head,
            "reason": reason, "updated": updated}
