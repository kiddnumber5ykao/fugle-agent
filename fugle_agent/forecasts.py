# ⬆️【要上傳 2026-06-08 08:25】forecasts.py — 新增第4盞「今天收盤」(介於下一小時與明天之間)
"""四盞預測:下一小時 / 今天收盤 / 明天 / 三天後 —— 用「同一份此刻最新快照」算。

設計原則:
  - 每盞只看「對它的時間長度有意義」的訊號(不同尺度用不同訊號 → 才有邏輯)。
  - 每個訊號投一票(偏上 +1 / 偏下 -1 / 沒意見 0 / 沒資料 None)。
  - 票一面倒 → 給方向 + 高信心;票打架 → 「說不準」。
  - 「怎麼辦」由三盞合成:遠的兩盞決定買賣方向,最近那盞決定動手時機。

這支只放「純函數」:輸入已抽好的數字(raw snapshot),輸出 (方向, 信心, 白話)。
真正即時抓資料、把報價/盤中/外資轉成這些數字,在 dashboard 組裝層做 → 方便單元測試。

★可調門檻集中在最上面。
"""

from __future__ import annotations

from typing import Any, Optional

# ───────────── 可調門檻 ─────────────
NH_MOVE_UP = 0.15     # 下一小時:最近約10分(後半,window 20)漲跌% 門檻 — 短窗口配小門檻
NH_MOVE_DN = -0.15
NH_VWAP_UP = 0.05     # 站上/跌破 VWAP 的%門檻(避免貼著線時亂投)
NH_VWAP_DN = -0.05
VOL_BIG = 1.2         # 放量門檻(近量 vs 平常量)
TM_RANGE_HI = 0.66    # 明天:今天收盤位置(0貼低~1貼高)
TM_RANGE_LO = 0.34
US_UP = 0.3           # 隔夜美股/期貨%門檻
US_DN = -0.3
TD_MA_UP = 0.0        # 三天後:站上/跌破均線%
TD_MA_DN = -2.0
RSI_OVERBOUGHT = 72   # 過熱 → 偏空一票(物極必反)
RSI_OVERSOLD = 30     # 超賣 → 偏多一票
TAKE_PROFIT_MIN = 6.0   # 持股賺超過這% + 動能轉弱 → 提示「見好就收」(短線設定,可調)


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (ValueError, TypeError):
        return None


def _vote_threshold(x: Optional[float], up: float, dn: float) -> Optional[int]:
    """x 高於 up → +1;低於 dn → -1;中間 → 0;None → None(不計票)。"""
    if x is None:
        return None
    if x >= up:
        return 1
    if x <= dn:
        return -1
    return 0


def _tally(votes: list[tuple[Optional[int], str, str]]) -> dict:
    """把一串 (票, 偏上時的白話, 偏下時的白話) 數成 方向+信心+白話。"""
    pos = neg = 0
    up_ph: list[str] = []
    dn_ph: list[str] = []
    for v, up, dn in votes:
        if v is None or v == 0:
            continue
        if v > 0:
            pos += 1
            if up:
                up_ph.append(up)
        else:
            neg += 1
            if dn:
                dn_ph.append(dn)
    nz = pos + neg
    s = pos - neg
    if nz == 0:
        return {"lean": "→ 說不準", "dir": 0, "conf": "低", "reason": "沒有明顯訊號"}
    if s == 0:
        # 票數打平 → 訊號打架,誠實說不準
        return {"lean": "→ 說不準", "dir": 0, "conf": "低",
                "reason": "、".join(up_ph + dn_ph) + ",訊號打架"}
    if s > 0:
        conf = "高" if (neg == 0 and pos >= 2) else ("中" if s >= 2 else "低")
        return {"lean": "↑ 偏上", "dir": 1, "conf": conf, "reason": "、".join(up_ph)}
    conf = "高" if (pos == 0 and neg >= 2) else ("中" if -s >= 2 else "低")
    return {"lean": "↓ 偏下", "dir": -1, "conf": conf, "reason": "、".join(dn_ph)}


# ===========================================================================
# 🔮 下一小時 —— 盤中微結構(現在這股力道撐不撐得住)
#   輸入:now_move_pct(近15-30分%)、pressure(-1/0/1 主動賣/中/買)、
#         vwap_pct(現價 vs VWAP %)、vol_ratio(近量/平常量)、
#         accel(-1/0/1 鈍化/中/加速)、mkt_now(-1/0/1 大盤此刻)
# ===========================================================================

def next_hour(s: dict) -> dict:
    mv = _num(s.get("now_move_pct"))       # 後半(最近約15分)漲跌% = 現在往哪走
    vr = _num(s.get("vol_ratio"))
    move_vote = _vote_threshold(mv, NH_MOVE_UP, NH_MOVE_DN)
    turn_vote = _as_vote(s.get("turn"))    # 剛轉向(+1剛翻上 / -1剛翻下)
    # 量只在「有方向時」才強化:放量 + 在漲 → +1;放量 + 在跌 → -1
    vol_vote = None
    if vr is not None and vr >= VOL_BIG and move_vote:
        vol_vote = 1 if move_vote > 0 else -1
    # 方向 = 抓「剛開始要往上/往下走」:用最近約15分的走勢(move,雙票主導)+『剛轉向』,
    # 配買賣力道、量、加速、大盤。不看「站上/跌破均價線」(那是跟整天平均比、接近跟開盤比,
    # 會把方向往整天漲跌拉,不符合『剛轉向』的意思)。
    votes = [
        (move_vote,                     "最近在往上走",   "最近在往下走"),
        (move_vote,                     "最近在往上走",   "最近在往下走"),   # 走勢雙票,主導方向
        (turn_vote,                     "剛開始往上翻",   "剛開始往下翻"),
        (_as_vote(s.get("pressure")),   "買盤較多",       "賣壓較重"),
        (vol_vote,                      "有量挺",         "量挺著跌"),
        (_as_vote(s.get("accel")),      "越走越快",       "走勢鈍化"),
        (_as_vote(s.get("mkt_now")),    "大盤順風",       "大盤逆風"),
    ]
    return _tally(votes)


# ===========================================================================
# 🕒 今天收盤 —— 從現在到 13:30,今天這根會怎麼收(介於「下一小時」和「明天」之間)
#   下一小時看「眼前一小時的微結構」,明天看「收盤定局 + 隔夜」;
#   這盞看「今天剩下的盤」:今天到現在的走勢 + 現價貼高/貼低 + 尾段方向 + 大盤今天。
#   用途:決定「今天該不該當沖了結 / 還是抱過夜」。
#   輸入:today_change_pct(今天到現在%)、close_strength(現價在今天高低位置 0~1)、
#         now_move_pct(最近這段方向)、mkt_today(-1/0/1 大盤今天)
# ===========================================================================

def today_close(s: dict) -> dict:
    tchg = _num(s.get("today_change_pct"))
    chg_vote = None if tchg is None else (1 if tchg > 0 else (-1 if tchg < 0 else 0))
    cs = _num(s.get("close_strength"))
    pos_vote = None
    if cs is not None:
        pos_vote = 1 if cs >= TM_RANGE_HI else (-1 if cs <= TM_RANGE_LO else 0)
    move_vote = _vote_threshold(_num(s.get("now_move_pct")), NH_MOVE_UP, NH_MOVE_DN)
    votes = [
        (chg_vote,                      "今天到現在走強", "今天到現在走弱"),
        (pos_vote,                      "貼著今天高檔",   "壓在今天低檔"),
        (move_vote,                     "尾段還在往上",   "尾段轉弱往下"),
        (_as_vote(s.get("mkt_today")),  "大盤今天偏多",   "大盤今天偏弱"),
    ]
    return _tally(votes)


# ===========================================================================
# 🌤️ 明天 —— 今天收尾 + 隔夜(信心天生最低)
#   輸入:close_strength(0~1 收盤在今天高低的位置)、today_change_pct、
#         today_vol_ratio、us_overnight_pct(費半/標普)、mkt_today(-1/0/1)
# ===========================================================================

def tomorrow(s: dict) -> dict:
    cs = _num(s.get("close_strength"))
    close_vote = None
    if cs is not None:
        close_vote = 1 if cs >= TM_RANGE_HI else (-1 if cs <= TM_RANGE_LO else 0)
    tchg = _num(s.get("today_change_pct"))
    chg_vote = None if tchg is None else (1 if tchg > 0 else (-1 if tchg < 0 else 0))
    tvr = _num(s.get("today_vol_ratio"))
    vol_vote = None
    if tvr is not None and chg_vote:
        vol_vote = (1 if chg_vote > 0 else -1) if tvr >= VOL_BIG else 0
    us_vote = _vote_threshold(_num(s.get("us_overnight_pct")), US_UP, US_DN)
    votes = [
        (close_vote,                    "今天收在高檔", "今天收在低檔"),
        (chg_vote,                      "今天收紅",     "今天收黑"),
        (vol_vote,                      "帶量",         "帶量殺"),
        (us_vote,                       "隔夜美股偏多", "隔夜美股偏弱"),
        (_as_vote(s.get("mkt_today")),  "大盤偏多",     "大盤偏弱"),
    ]
    out = _tally(votes)
    # 明天受隔夜變數大 → 信心上限壓一級(高→中),老實一點
    if out["conf"] == "高":
        out["conf"] = "中"
    return out


# ===========================================================================
# 📅 三天後 —— 波段動能 + 大錢方向(最值得信)
#   輸入:dist_ma_pct(離均線%)、rsi、foreign_dir(-1/0/1 外資賣/中/買)、
#         vol_ratio_5_20(量價配合)、mkt_swing(-1/0/1 大盤波段)、trend_up(備援)
# ===========================================================================

def three_day(s: dict) -> dict:
    trend_vote = _vote_threshold(_num(s.get("dist_ma_pct")), TD_MA_UP, TD_MA_DN)
    rsi = _num(s.get("rsi"))
    rsi_vote = None
    if rsi is not None:
        if rsi >= RSI_OVERBOUGHT:
            rsi_vote = -1            # 過熱 → 容易回檔
        elif rsi <= RSI_OVERSOLD:
            rsi_vote = 1             # 超賣 → 容易反彈
        else:
            rsi_vote = 0
    # 量價配合:有方向(用趨勢當方向)時放量加分、背離扣分
    vp = _num(s.get("vol_ratio_5_20"))
    vp_vote = None
    if vp is not None and trend_vote:
        vp_vote = (1 if trend_vote > 0 else -1) if vp >= VOL_BIG else 0
    votes = [
        (trend_vote,                    "趨勢向上",     "趨勢向下"),
        (rsi_vote,                      "跌深、容易反彈", "過熱、容易回檔"),
        (_as_vote(s.get("foreign_dir")), "外資在買",    "外資在賣"),
        (vp_vote,                       "量價配合",     "量價背離"),
        (_as_vote(s.get("mkt_swing")),  "大盤多頭",     "大盤空頭"),
    ]
    return _tally(votes)


def _as_vote(v: Any) -> Optional[int]:
    """把已經是 -1/0/1 的輸入安全轉成票;None / 非數字 → None。"""
    n = _num(v)
    if n is None:
        return None
    if n > 0:
        return 1
    if n < 0:
        return -1
    return 0


# ===========================================================================
# 👉 怎麼辦 —— 遠的兩盞決定方向,最近那盞決定時機
# ===========================================================================

def combined_action(nh: dict, td_close: dict, tm: dict, td: dict, *,
                    is_holding: bool, headwind: bool = False,
                    pnl_pct: float | None = None) -> str:
    """nh/td_close/tm/td 是四盞預測結果(含 dir: 1/0/-1)。pnl_pct=目前損益%(持股才有)。
    回一句白話「怎麼辦」。遠的兩盞(明天/三天後)決定方向,近的兩盞(下一小時/今天收盤)決定時機。"""
    near = int(nh.get("dir", 0))          # 下一小時
    tc_dir = int(td_close.get("dir", 0))  # 今天收盤
    tm_dir = int(tm.get("dir", 0))        # 明天
    td_dir = int(td.get("dir", 0))        # 三天後
    # 遠的方向:三天後權重 2、明天權重 1
    far = td_dir * 2 + tm_dir
    # 近期是否「至少有一盞站出來偏多」(下一小時 / 今天收盤 / 明天)。都沒表態 → 近期還不明朗。
    near_supports_up = (near > 0) or (tc_dir > 0) or (tm_dir > 0)

    if is_holding:
        if far <= -2:
            if near > 0:
                return "🔴 想減/出、可等這波衝高一點再出"
            return "🔴 考慮減碼或出場"
        # 見好就收:已經賺一波(pnl_pct 夠高)+ 近期動能轉弱(此刻在殺 或 明天偏下),
        # 就算波段(三天後)還沒翻空,也提示先落袋一部分,別把賺到的吐回去。
        in_good_profit = pnl_pct is not None and pnl_pct >= TAKE_PROFIT_MIN
        fading = (near < 0) or (tc_dir < 0) or (tm_dir < 0)
        if in_good_profit and fading:
            return f"🟠 見好就收、先獲利了結一部分(已賺 {pnl_pct:.0f}%、動能轉弱)"
        if far >= 2:
            if near < 0:
                return "🟢 抱著、別追加(此刻在殺、等它穩)"
            if far >= 3:
                return _brake_buy("🟢 抱著、可考慮加碼", headwind)
            if near > 0:
                return "🟢 抱著、走勢有撐"          # 三天後偏多 + 下一小時也偏上
            return "🟡 抱著、續抱觀察(近期方向還不明)"  # 只有三天後偏多、近期說不準
        return "🟡 抱著觀察、訊號還不明"

    # 追蹤(找進場)
    if far >= 2:
        if near < 0:
            return "🟡 想進、等它止穩再進(此刻在殺)"
        # 只有最遠那盞偏多、近兩盞(下一小時/明天)都還沒表態 → 別急著進
        if not near_supports_up:
            return "🟡 方向偏多,但近期(下一小時/明天)還沒表態、再等等"
        return _brake_buy("🟢 可考慮進場" + ("(偏積極)" if far >= 3 else ""), headwind)
    if far <= -2:
        return "🔴 先別進、方向偏空"
    return "🟡 再等等、沒明顯機會"


def _brake_buy(text: str, headwind: bool) -> str:
    """大盤逆風時對「買進類」動作踩煞車。進場直接降級成『等大盤穩再進』,加碼則加一句提醒。"""
    if not headwind:
        return text
    if "進場" in text:
        return "🟡 想進、但大盤逆風,等大盤穩一點再進"
    return text + "　🌡️ 但大盤逆風,想買的話等大盤穩一點再進"


def all_three(snapshot: dict, *, is_holding: bool, headwind: bool = False,
              pnl_pct: float | None = None) -> dict:
    """一次算四盞 + 怎麼辦。snapshot 是抽好的此刻最新數字。pnl_pct=目前損益%(見好就收用)。
    (函式名沿用 all_three 不改,避免動到所有呼叫端;實際回四盞。)"""
    nh = next_hour(snapshot)
    tc = today_close(snapshot)
    tm = tomorrow(snapshot)
    td = three_day(snapshot)
    return {
        "next_hour": nh,
        "today_close": tc,
        "tomorrow": tm,
        "three_day": td,
        "action": combined_action(nh, tc, tm, td, is_holding=is_holding,
                                  headwind=headwind, pnl_pct=pnl_pct),
    }
