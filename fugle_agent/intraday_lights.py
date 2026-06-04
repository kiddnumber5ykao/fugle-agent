# ✅【本次上傳批次：2026-06-04 三盞預測燈版 v1】intraday_lights.py — 盤中微結構 helper
"""盤中即時燈號 — 純計算核心。

三盞燈的分工(從最即時 → 最穩):
  1) 此刻燈  — 現在這一刻在衝還在殺(近 N 分鐘方向)。資料:盤中分鐘K / ticks。
  2) 今天燈  — 今天一整天到現在強不強(漲跌%、在今天高低的位置、離開盤)。資料:即時報價。
  3) 這陣子燈 — 5~10 天趨勢(在 tools.py 的 _momentum_light,走每日收盤)。這支不處理。

這支只放「純函數」:輸入已經抓好的報價 / 分鐘序列,輸出 (燈號, 白話, 數字)。
真正抓資料 + 接畫面在 dashboard 那邊,方便單元測試、也好調參數。

★可調參數集中在最上面,之後嫌太敏感/太鈍直接改這裡。
"""

from __future__ import annotations

from typing import Any, Optional

# ───────────────────────── 可調參數 ─────────────────────────
# 此刻燈:看「現在 vs 幾分鐘前」。越短越即時、越敏感。
NOW_LOOKBACK_MIN = 5        # 回看幾分鐘
NOW_UP_PCT = 0.30          # 漲超過這個% → 在衝
NOW_DOWN_PCT = -0.30       # 跌超過這個% → 在殺
# 今天燈門檻(分數制,見 today_light)
TODAY_STRONG = 2
TODAY_WEAK = -2


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


# ===========================================================================
# 今天燈 — 從即時報價算(便宜,一次 quote 就有)
# ===========================================================================

def today_light(quote: dict) -> dict:
    """今天一整天到現在的強弱。回 {light, reason, change_pct, range_pos, vs_open_pct}。

    看三件事:
      - 現在比昨收漲跌%(change_pct)
      - 現在卡在「今天最高～最低」的哪個位置(range_pos:0=貼低點,1=貼高點)
      - 開盤後是站上去還是垮下來(vs_open_pct)
    """
    if not isinstance(quote, dict) or quote.get("error"):
        return {"light": "⚪ 資料不足", "reason": "抓不到即時報價",
                "change_pct": None, "range_pos": None, "vs_open_pct": None}

    last = _f(quote.get("lastPrice")) or _f(quote.get("closePrice")) or _f(quote.get("price"))
    prev = _f(quote.get("previousClose"))
    op = _f(quote.get("openPrice")) or _f(quote.get("open"))
    hi = _f(quote.get("highPrice")) or _f(quote.get("high"))
    lo = _f(quote.get("lowPrice")) or _f(quote.get("low"))

    if last is None:
        return {"light": "⚪ 資料不足", "reason": "報價沒有現價",
                "change_pct": None, "range_pos": None, "vs_open_pct": None}

    # 漲跌%:優先用報價自帶的 changePercent,沒有就自己用昨收算
    chg = _f(quote.get("changePercent"))
    if chg is None and prev:
        chg = (last / prev - 1) * 100

    # 在今天高低區間的位置
    range_pos = None
    if hi is not None and lo is not None and hi > lo:
        range_pos = (last - lo) / (hi - lo)
        range_pos = max(0.0, min(1.0, range_pos))

    # 離開盤
    vs_open = None
    if op:
        vs_open = (last / op - 1) * 100

    # 評分
    score = 0
    if chg is not None:
        if chg >= 2:       score += 2
        elif chg >= 0.5:   score += 1
        elif chg <= -2:    score -= 2
        elif chg <= -0.5:  score -= 1
    if range_pos is not None:
        if range_pos >= 0.7:   score += 1
        elif range_pos <= 0.3: score -= 1
    if vs_open is not None:
        if vs_open >= 0.5:     score += 1
        elif vs_open <= -0.5:  score -= 1

    if score >= TODAY_STRONG:
        light = "🟢 今天強"
    elif score <= TODAY_WEAK:
        light = "🔴 今天弱"
    else:
        light = "🟡 普通"

    # 白話
    bits = []
    if chg is not None:
        bits.append(f"比昨天{'漲' if chg >= 0 else '跌'} {abs(chg):.1f}%")
    if range_pos is not None:
        if range_pos >= 0.7:   bits.append("貼在今天高點附近")
        elif range_pos <= 0.3: bits.append("壓在今天低點附近")
        else:                  bits.append("在今天高低中間")
    if vs_open is not None:
        bits.append("站在開盤價上面" if vs_open >= 0 else "跌破開盤價")
    reason = "、".join(bits) if bits else "資料不足"

    return {"light": light, "reason": reason,
            "change_pct": round(chg, 2) if chg is not None else None,
            "range_pos": round(range_pos, 2) if range_pos is not None else None,
            "vs_open_pct": round(vs_open, 2) if vs_open is not None else None}


# ===========================================================================
# 此刻燈 — 從盤中分鐘序列算(現在 vs 幾分鐘前的方向)
# ===========================================================================

def now_light(series: list[tuple[float, float]] | None, quote: dict | None = None) -> dict:
    """現在這一刻的方向。回 {light, reason, move_pct, basis}。

    series:盤中近一段的 (epoch秒, 價格),時間由舊到新排序。
    優先用 series 算「現在 vs NOW_LOOKBACK_MIN 分鐘前」;
    series 不夠(冷門股、剛開盤)就退而用報價的「現在 vs 開盤」當近似。
    """
    last = None
    ref = None
    basis = ""

    # 1) 用分鐘序列:找「現在」與「N 分鐘前」兩點
    if series:
        pts = [(t, p) for t, p in series if t is not None and p is not None]
        pts.sort(key=lambda x: x[0])
        if len(pts) >= 2:
            last_t, last = pts[-1]
            cutoff = last_t - NOW_LOOKBACK_MIN * 60
            ref = None
            for t, p in pts:                 # 第一個「不早於 cutoff」的點
                if t >= cutoff:
                    ref = p
                    break
            if ref is None:
                ref = pts[0][1]              # 都比 cutoff 晚 → 用最舊那點
            basis = f"近 {NOW_LOOKBACK_MIN} 分鐘"

    # 2) 退路:用報價「現在 vs 開盤」
    if (last is None or ref is None or ref == 0) and isinstance(quote, dict):
        lp = _f(quote.get("lastPrice")) or _f(quote.get("closePrice")) or _f(quote.get("price"))
        op = _f(quote.get("openPrice")) or _f(quote.get("open"))
        if lp is not None and op:
            last, ref, basis = lp, op, "對比開盤"

    if last is None or ref is None or ref == 0:
        return {"light": "⚪ 資料不足", "reason": "抓不到盤中即時走勢",
                "move_pct": None, "basis": ""}

    move = (last / ref - 1) * 100
    if move >= NOW_UP_PCT:
        light, word = "🟢 在衝", "正在往上拉"
    elif move <= NOW_DOWN_PCT:
        light, word = "🔴 在殺", "正在往下殺"
    else:
        light, word = "🟡 沒動", "卡住、沒什麼動"
    reason = f"{basis}{word} {move:+.2f}%"
    return {"light": light, "reason": reason, "move_pct": round(move, 2), "basis": basis}


# ===========================================================================
# 把 ticks / 分鐘K 統一整理成 now_light 要的 (epoch秒, 價) 序列
# ===========================================================================

def series_from_candles(candles: dict | None) -> list[tuple[float, float]]:
    """盤中分鐘K → [(epoch秒, close), ...]。各種時間欄位/單位都吃。"""
    out: list[tuple[float, float]] = []
    rows = (candles or {}).get("data") if isinstance(candles, dict) else None
    if not rows:
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        price = _f(r.get("close")) or _f(r.get("price")) or _f(r.get("last"))
        t = _to_epoch_sec(r.get("date") or r.get("time") or r.get("timestamp") or r.get("at"))
        if price is not None and t is not None:
            out.append((t, price))
    out.sort(key=lambda x: x[0])
    return out


def series_from_ticks(ticks: dict | None) -> list[tuple[float, float]]:
    """盤中逐筆 → [(epoch秒, price), ...]。"""
    out: list[tuple[float, float]] = []
    rows = (ticks or {}).get("data") if isinstance(ticks, dict) else None
    if not rows:
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        price = _f(r.get("price")) or _f(r.get("close"))
        t = _to_epoch_sec(r.get("time") or r.get("date") or r.get("timestamp"))
        if price is not None and t is not None:
            out.append((t, price))
    out.sort(key=lambda x: x[0])
    return out


def vwap_pct_from_candles(candles: dict | None, last_price: float | None) -> Optional[float]:
    """用盤中分鐘K算 VWAP(成交量加權均價),回「現價 vs VWAP 的%」。
    站上 VWAP → 正、跌破 → 負。算不出回 None。"""
    rows = (candles or {}).get("data") if isinstance(candles, dict) else None
    if not rows or last_price is None:
        return None
    num = 0.0
    den = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        p = _f(r.get("close")) or _f(r.get("price"))
        v = _f(r.get("volume")) or _f(r.get("size")) or _f(r.get("tradeVolume"))
        if p is None or v is None or v <= 0:
            continue
        num += p * v
        den += v
    if den <= 0:
        return None
    vwap = num / den
    if vwap <= 0:
        return None
    return (last_price / vwap - 1) * 100


def pressure_from_ticks(ticks: dict | None) -> Optional[int]:
    """從盤中逐筆的「主動買/主動賣」標記算買賣力道。
    tickType 1=主動買、2=主動賣(Fugle 慣例)。回 -1/0/1;沒資料回 None。"""
    rows = (ticks or {}).get("data") if isinstance(ticks, dict) else None
    if not rows:
        return None
    buy = sell = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        tt = r.get("tickType")
        sz = _f(r.get("size")) or _f(r.get("volume")) or 1.0
        if tt in (1, "1"):
            buy += sz
        elif tt in (2, "2"):
            sell += sz
    tot = buy + sell
    if tot <= 0:
        return None
    net = (buy - sell) / tot       # -1(全賣) ~ +1(全買)
    if net >= 0.15:
        return 1
    if net <= -0.15:
        return -1
    return 0


def now_move_and_accel(series: list[tuple[float, float]] | None,
                       lookback_min: int = 20) -> tuple[Optional[float], Optional[int]]:
    """從盤中分鐘序列算 (近 lookback 分鐘漲跌%, 加速票)。
    加速票:把這段切前後兩半,後半斜率 > 前半同向 → +1(越來越快);反向轉弱 → -1。"""
    if not series:
        return (None, None)
    pts = sorted([(t, p) for t, p in series if t is not None and p is not None],
                 key=lambda x: x[0])
    if len(pts) < 2:
        return (None, None)
    last_t, last_p = pts[-1]
    cutoff = last_t - lookback_min * 60
    window = [(t, p) for t, p in pts if t >= cutoff] or pts
    ref_p = window[0][1]
    move = (last_p / ref_p - 1) * 100 if ref_p else None

    accel = None
    if len(window) >= 4:
        mid = len(window) // 2
        first_half = (window[mid][1] / window[0][1] - 1) if window[0][1] else 0
        second_half = (window[-1][1] / window[mid][1] - 1) if window[mid][1] else 0
        if abs(second_half) > abs(first_half) * 1.15:
            accel = 1 if second_half * (move or 0) >= 0 else 0
        elif abs(second_half) < abs(first_half) * 0.6:
            accel = -1
        else:
            accel = 0
    return (move, accel)


def _to_epoch_sec(v: Any) -> Optional[float]:
    """把各種時間表示轉成 epoch 秒。吃 epoch 秒/毫秒/微秒/奈秒、ISO 字串。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x <= 0:
            return None
        if x >= 1e18:   return x / 1e9   # ns
        if x >= 1e15:   return x / 1e6   # us
        if x >= 1e12:   return x / 1e3   # ms
        return x                          # s
    s = str(v).strip()
    if not s:
        return None
    if s.lstrip("-").isdigit():
        return _to_epoch_sec(int(s))
    try:
        import datetime as _dt
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None
