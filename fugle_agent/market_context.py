# ⬆️【要上傳 2026-06-05 00:53】market_context.py — 大盤永遠講台股加權(拿掉盤前改講美股)
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


def _fugle_data_time(q: dict) -> str:
    """從 Fugle 報價抓『資料本身的時間』(不是系統時間)→ 台北 YYYY-MM-DD HH:MM:SS。
    抓不到回 ""。相容多種格式:epoch 秒/毫秒/微秒/奈秒,或 ISO 字串。"""
    def _from_epoch(val) -> datetime.datetime | None:
        try:
            v = float(val)
        except (ValueError, TypeError):
            return None
        if v <= 0:
            return None
        if v >= 1e18:      v /= 1e9   # 奈秒
        elif v >= 1e15:    v /= 1e6   # 微秒
        elif v >= 1e12:    v /= 1e3   # 毫秒
        # 否則當作秒
        try:
            return (datetime.datetime.fromtimestamp(v, datetime.timezone.utc)
                    .astimezone(datetime.timezone(datetime.timedelta(hours=8))))
        except (OSError, ValueError, OverflowError):
            return None

    if not isinstance(q, dict):
        return ""
    cands = [q.get("lastUpdated"), q.get("at"), q.get("time")]
    lt = q.get("lastTrade")
    if isinstance(lt, dict):
        cands.append(lt.get("time"))
    for c in cands:
        if c is None:
            continue
        if isinstance(c, (int, float)):
            dt = _from_epoch(c)
            if dt:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(c, str) and c.strip():
            s = c.strip()
            if s.lstrip("-").isdigit():        # 純數字字串 → 當 epoch
                dt = _from_epoch(s)
                if dt:
                    return dt.strftime("%Y-%m-%d %H:%M:%S")
            try:                                # ISO 字串
                dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                dt = dt.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
    return ""


def _live_index_today(today_str: str) -> tuple[float | None, str, str]:
    """盤中抓『今天』的即時加權指數。回 (指數, 日期, 資料時間到秒) 或 (None, "", "")。
    1) Fugle 即時報價 IX0001(跟你個股同源、不延遲,且帶資料時間戳)。
    2) 退 yfinance ^TWII(延遲 ~15 分,只有日期、沒有到秒的盤中時間)。"""
    # 1) Fugle 即時報價
    try:
        from fugle_agent.client import FugleClient
        q = FugleClient().quote(_FUGLE_TAIEX)
        if isinstance(q, dict) and not q.get("error"):
            level = None
            for k in ("lastPrice", "closePrice", "price", "last"):
                v = q.get(k)
                if isinstance(v, (int, float)) and v:
                    level = float(v)
                    break
                if isinstance(v, str) and v.strip():
                    try:
                        level = float(v.replace(",", ""))
                        break
                    except ValueError:
                        pass
            if level is not None:
                qd = str(q.get("date", ""))[:10]
                return level, (qd or today_str), _fugle_data_time(q)
    except Exception:
        pass
    # 2) yfinance ^TWII 退路(沒有到秒的資料時間)
    try:
        r = us_market.quote(_TWII)
        if isinstance(r, dict) and not r.get("error"):
            level = r.get("lastPrice")
            qd = str(r.get("asOf", ""))[:10]
            if isinstance(level, (int, float)) and qd and qd >= today_str:
                return float(level), qd, ""
    except Exception:
        pass
    return None, "", ""


def _twii_signals() -> dict | None:
    """回 {close, ma10, change_pct, above_ma10, date} 或 None(抓不到)。

    盤中:今天的指數用即時報價(才不會卡在昨天的日K);10 日線與昨收用日K。
    抓不到即時 → 退回最後一根日K(舊行為,至少誠實標昨天日期)。"""
    bars = _index_bars()
    if len(bars) < 2:
        return None
    today_str = _now_tw().strftime("%Y-%m-%d")
    closes = [float(b["close"]) for b in bars]
    dates = [str(b.get("date", ""))[:10] for b in bars]

    # 昨收 = 最後一根「日期 < 今天」的 bar(若日K還沒有今天,就是 closes[-1])
    prev_close = None
    for c, d in zip(reversed(closes), reversed(dates)):
        if d and d < today_str:
            prev_close = c
            break
    if prev_close is None:
        prev_close = closes[-2]

    # 10 日線基準:只用「已完成日」的收盤(排除今天那根,若日K已含今天)
    completed = [c for c, d in zip(closes, dates) if d and d < today_str] or closes
    ma10 = sum(completed[-10:]) / min(len(completed), 10)

    # 今天的指數:先試即時;失敗才退回最後一根日K
    last, date_used, data_time = _live_index_today(today_str)
    if last is None or date_used < today_str or abs(last - prev_close) <= 1e-6:
        last = closes[-1]
        date_used = dates[-1]
        data_time = ""   # 退回日K → 沒有到秒的盤中時間,只剩日期

    return {
        "close": last,
        "ma10": ma10,
        "data_time": data_time,
        "change_pct": (last / prev_close - 1) * 100 if prev_close else 0.0,
        "above_ma10": last >= ma10,
        "date": date_used,
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
    # 大盤橫幅永遠講「台股加權指數」(不分盤前盤後;沒開盤就顯示最近一個收盤的加權)。
    # 美股有獨立一條,不在這裡混進來。
    s = _twii_signals()
    if not s:
        return {"light": "🟡 普通", "phase": "大盤", "is_headwind": False,
                "reason": "抓不到加權指數,當作普通", "updated": updated}
    chg = s["change_pct"]
    # 顯示「資料本身的時間」(不是系統時間):有即時時間戳就用到秒,
    # 退回日K時只剩日期(收盤後/抓不到盤中即時時)。
    when = s.get("data_time") or s.get("date", "")
    dtag = f"(資料 {when})" if when else ""
    if s["above_ma10"] and chg >= 0:
        light, head = "🟢 順風", False
        reason = f"加權站上 10 日線、{chg:+.1f}%,大盤偏多,順風 {dtag}"
    elif (not s["above_ma10"]) and chg < 0:
        light, head = "🔴 逆風", True
        reason = f"加權跌破 10 日線、{chg:+.1f}%,大盤偏弱,買進保守點 {dtag}"
    else:
        light, head = "🟡 普通", False
        reason = f"加權在均線附近、{chg:+.1f}%,方向不明 {dtag}"
    return {"light": light, "phase": "大盤", "is_headwind": head,
            "reason": reason, "updated": updated,
            "data_time": s.get("data_time", ""), "data_date": s.get("date", "")}
