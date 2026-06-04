# ⬆️【要上傳 2026-06-04 22:32】dashboard.py — 卡片改版 + 按鈕進度 + 上市/上櫃標示 + 拿掉看更新狀況
"""手機儀表板 — 「加油好嗎？」首頁。

讀 Google Sheet 的股票部位 / 追蹤清單,渲染成手機友善的卡片:
  • 持有 / 追蹤 切換(預設持有)
  • 全更新 / 盤中更新 按鈕(觸發 GitHub workflow)
  • 持有才有的總市值 / 總損益 / 已實現
  • 「該做什麼」摘要
  • 依「該做啥」優先序排序的卡片,點開看細節(燈號 / 目標價 / 描述 / 鮮度)

render() 由 app.py 呼叫,並傳入觸發 workflow / 按鈕狀態的 callback。
"""
from __future__ import annotations

import datetime
import os

import streamlit as st

from fugle_agent import sheets

# ---- 燈號 / 動作 顏色 ----
_DOT = {"🟢": "#639922", "🟡": "#BA7517", "🔴": "#E24B4A", "⚪": "#B4B2A9"}


def _tier(light: str) -> str:
    # 動能燈 5 段:🔥(強勢)歸🟢、🟠(偏弱)歸🔴
    s = str(light or "")
    if "⚪" in s or "資料不足" in s or not s.strip():
        return "⚪"
    if "🔥" in s or "🟢" in s:
        return "🟢"
    if "🟠" in s or "🔴" in s:
        return "🔴"
    if "🟡" in s:
        return "🟡"
    return "⚪"


# 基本面燈號顯示成「色 + 詞」,避免一堆同色 🟢 分不清誰是誰
_FUND_WORD = {"🟢": "好", "🟡": "普通", "🔴": "偏弱", "⚪": "沒分析"}


def _fund_light_label(light: str) -> str:
    s = str(light or "").strip()
    if not s:
        return "⚪ 沒分析"
    base = _tier(s)                       # 收斂成 🟢/🟡/🔴/⚪
    word = _FUND_WORD.get(base, "")
    return f"{base} {word}".strip() if word else s


def _g(row: dict, *names: str) -> str:
    for n in names:
        v = row.get(n)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _num(v) -> float | None:
    s = str(v or "").replace(",", "").replace("$", "").replace("%", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _action_rank(advice: str) -> tuple:
    """排序鍵:最該動(0) → 軟動作/再等等(1) → 抱著(2) → 不要/先別(3)
    → 等分析(4)。同層:賣/減碼 優先於買。
    動作詞:趕快買/趕快賣/停損/先減碼=0;可以買/再等等/留意/偏減碼=1;
            抱緊加碼/續抱*/抱著等=2;不要買/先別買=3;⏳等…=4。"""
    a = str(advice or "").split("[")[0].strip()   # 只看動作詞,別被[說明]裡的字干擾
    if (a.startswith("趕快") or a.startswith("停損")
            or a.startswith("先減碼") or a.startswith("賣一批")):
        tier = 0
    elif (a.startswith("可以買") or a.startswith("再等等")
          or a.startswith("留意") or a.startswith("偏減碼")):
        tier = 1
    elif a.startswith("抱") or a.startswith("續抱"):
        tier = 2
    elif a.startswith("不要") or a.startswith("先別"):
        tier = 3
    elif "等基本面" in a or "等技術" in a or a.startswith("⏳"):
        tier = 4
    else:
        tier = 5
    sub = 0 if ("賣" in a or "減碼" in a or "停損" in a) else 1
    return (tier, sub)


def _action_emoji(advice: str) -> str:
    a = str(advice or "").split("[")[0].strip()   # 只看動作詞,別被[說明]裡的賣/買字干擾
    if a.startswith("不要") or a.startswith("先別"):
        return "🟡"
    if "賣" in a or "停損" in a or "減碼" in a:
        return "🔴"
    if "買" in a or a.startswith("抱") or a.startswith("續抱"):
        return "🟢"
    if a.startswith("⏳") or "等" in a or a.startswith("留意"):
        return "🟡"
    return "⚪"


def _action_icon(advice: str) -> str:
    """每個動作詞給一個專屬 icon,讓不同的字一眼就不一樣(只看動作詞,不看說明)。"""
    a = str(advice or "").split("[")[0].strip()
    # 賣 / 減碼 側
    if a.startswith("趕快賣"):
        return "🔴"
    if a.startswith("停損"):
        return "🛑"
    if a.startswith("先減碼") or a.startswith("偏減碼"):
        return "✂️"
    if a.startswith("留意"):
        return "👀"
    # 買 側
    if a.startswith("趕快買") or a.startswith("趕快再買"):
        return "🚀"
    if a.startswith("可以買"):
        return "🟢"
    if a.startswith("再等等買"):
        return "🕐"
    if a.startswith("再等等"):
        return "⏸️"
    # 抱 側(抱緊加碼要先判,因為它也 startswith 抱)
    if a.startswith("抱緊加碼"):
        return "💪"
    if a.startswith("抱著等"):
        return "😴"
    if a.startswith("續抱") or a.startswith("抱"):
        return "🤲"
    # 不買 / 等
    if a.startswith("不要"):
        return "🚫"
    if a.startswith("先別"):
        return "✋"
    if a.startswith("⏳") or "等基本面" in a or "等技術" in a:
        return "⏳"
    return "⚪"


def _action_short(advice: str) -> str:
    """卡片標題用的短動作(去掉 [] 原因)。"""
    a = str(advice or "").split("[")[0].strip()
    return a or "—"


def _action_reason(advice: str) -> str:
    a = str(advice or "")
    if "[" in a and "]" in a:
        return a[a.index("[") + 1:a.rindex("]")]
    return ""


def _rel_time(ts: str, stale_min: int) -> tuple[str, bool]:
    """把『更新時間』(YYYY-MM-DD HH:MM:SS,台北)轉成
    『剛剛 / X分前 / X小時前 / X天前』+ 是否過久。
    只有日期(舊資料、沒時分)時,當天 00:00 算。"""
    s = str(ts or "").strip()
    if not s:
        return ("尚未更新", True)
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(s[:19], fmt)
            break
        except ValueError:
            dt = None
    if dt is None:
        return (s, False)
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).replace(tzinfo=None)
    mins = (now - dt).total_seconds() / 60
    if mins < 0:
        mins = 0
    stale = mins > stale_min
    if mins < 1:
        txt = "剛剛"
    elif mins < 60:
        txt = f"{int(mins)}分前"
    elif mins < 60 * 24:
        txt = f"{int(mins // 60)}小時前"
    else:
        txt = f"{int(mins // (60 * 24))}天前"
    return (txt, stale)


def _dots(lights: list[str]) -> str:
    out = ""
    for lg in lights:
        out += (f'<span style="display:inline-block;width:11px;height:11px;'
                f'border-radius:50%;background:{_DOT[_tier(lg)]};'
                f'margin-right:5px;vertical-align:-1px"></span>')
    return out


def _pill(text: str, kind: str) -> str:
    bg = {"danger": "var(--background-color)", }.get(kind, "")
    colors = {
        "danger": ("#FCEBEB", "#A32D2D"),
        "success": ("#EAF3DE", "#3B6D11"),
        "gray": ("#F1EFE8", "#5F5E5A"),
    }.get(kind, ("#F1EFE8", "#5F5E5A"))
    return (f'<span style="display:inline-block;padding:2px 9px;border-radius:8px;'
            f'font-size:13px;font-weight:500;background:{colors[0]};color:{colors[1]}">'
            f'{text}</span>')


def _pill_kind(advice: str) -> str:
    a = str(advice or "").split("[")[0].strip()   # 只看動作詞
    if a.startswith("不要") or a.startswith("先別"):
        return "gray"
    if "賣" in a or "停損" in a or "減碼" in a:
        return "danger"
    if "買" in a or a.startswith("抱") or a.startswith("續抱"):
        return "success"
    return "gray"


# 每個「動作詞」的優先序(最優先→最不優先)。比對時用 startswith,
# 所以較長/較專一的要排在較短的前面(例:續抱別加 要在 續抱 之前)。
_ACTION_ORDER = [
    "趕快賣", "停損", "先減碼", "趕快買", "偏減碼",
    "可以買", "再等等買", "留意", "再等等",
    "抱緊加碼", "續抱別加", "續抱但別貪", "續抱", "抱著等",
    "先別買", "不要買",
    "⏳", "⚪",
]


def _action_order_key(advice: str) -> int:
    """回傳該動作詞在優先序裡的名次(越小越優先)。"""
    head = _action_short(advice)
    for i, w in enumerate(_ACTION_ORDER):
        if head.startswith(w):
            return i
    return len(_ACTION_ORDER)


def _render_cards(rows: list[dict], card_fn) -> None:
    """依「動作詞」優先序排序,並用『動作詞本身』當分組標題(詞不同就分開)。"""
    rows.sort(key=lambda r: _action_order_key(_g(r, "我該做啥", "綜合建議")))
    st.markdown('<div class="gyh-card">', unsafe_allow_html=True)
    last_head = None
    for r in rows:
        adv = _g(r, "我該做啥", "綜合建議")
        head = _action_short(adv)
        if head != last_head:
            st.markdown(
                f'<div style="margin:14px 0 4px;font-weight:600;font-size:13px;'
                f'color:#5F5E5A">{_action_icon(adv)} {head}</div>',
                unsafe_allow_html=True)
            last_head = head
        card_fn(r)
    st.markdown('</div>', unsafe_allow_html=True)


@st.cache_data(ttl=60, show_spinner=False)
def _load_today_changes() -> list[dict]:
    try:
        from fugle_agent import sheets_writer as _sw
        r = _sw.get_changes_today()
        return r.get("changes", []) if r.get("ok") else []
    except Exception:
        return []


def _changes_html() -> tuple[int, str]:
    """回 (今天變化筆數, 內容 HTML) — 給「👀 今天要注意的」收合區用。"""
    changes = _load_today_changes()
    groups = [
        ("買", "🟢 開始有起色的", "#639922"),
        ("賣", "🔴 開始轉壞的", "#E24B4A"),
        ("看", "🟡 量有異常的", "#BA7517"),
    ]
    body = ""
    for key, title, color in groups:
        items = [c for c in changes if (c.get("dir") or "看") == key]
        if not items:
            continue
        body += (f'<div style="display:flex;align-items:center;gap:6px;margin:10px 0 4px">'
                 f'<span style="width:9px;height:9px;border-radius:50%;background:{color};'
                 f'display:inline-block"></span><span style="font-size:13px;font-weight:500;'
                 f'color:{color}">{title}</span></div>')
        for c in items:
            t = str(c.get("time") or "")[11:16]
            body += (f'<div style="border-top:0.5px solid rgba(127,127,127,.2);display:flex;'
                     f'justify-content:space-between;gap:10px;padding:7px 0">'
                     f'<div><div style="font-size:14px;font-weight:500">{c.get("symbol","")} '
                     f'{c.get("name","")}</div><div style="font-size:13px;color:#7d7d7d;'
                     f'line-height:1.5">{c.get("msg","")}</div></div>'
                     f'<span style="font-size:12px;color:#9a9a9a;white-space:nowrap">{t}</span></div>')
    if not body:
        body = '<div style="font-size:13px;color:#7d7d7d;padding:4px 0">今天還沒什麼動靜 😌</div>'
    return (len(changes), body)


# 動作詞 → (icon, 白話標籤, 是否「要動手」)。全部收合分組,is_act 不再用來展開。
def _plain_action(advice: str) -> tuple[str, str, bool]:
    a = _action_short(advice)
    # ── 賣:三級 ──
    if a.startswith("賣一半"):
        return ("🔻", "賣一半", True)
    if a.startswith("賣1/3") or a.startswith("賣1／3") or a.startswith("賣三分"):
        return ("✂️", "賣1/3", True)
    if (a.startswith("全部賣") or a.startswith("全賣") or a.startswith("停損")
            or a.startswith("該賣") or a.startswith("趕快賣")):
        return ("🛑", "全部賣掉", True)
    # ── 加碼(持股) ──
    if a.startswith("還能再買") or a.startswith("可以加碼") or a.startswith("抱緊加碼"):
        return ("💪", "還能再買一點", True)
    # ── 買(追蹤) ──
    if a.startswith("可以買") or a.startswith("趕快買"):
        return ("🚀", "可以買", True)
    if a.startswith("先買一點") or a.startswith("慢慢買"):
        return ("🟢", "先買一點", True)
    # ── 不用動手 ──
    if (a.startswith("抱") or a.startswith("續抱") or a.startswith("盯緊")
            or a.startswith("留意")):
        return ("🤲", "抱著就好", False)
    if a.startswith("再等等"):
        return ("⏸️", "再等等", False)
    if a.startswith("先別") or a.startswith("不要"):
        return ("🚫", "先別碰", False)
    # 其餘(⏳ 等分析、空白…)
    return ("⚪", "資料不足", False)


# 由上到下的優先序(急→緩;持股與追蹤共用一張表,各頁只會出現自己的)
_PLAIN_ORDER = ["全部賣掉", "賣一半", "賣1/3",
                "可以買", "先買一點", "還能再買一點",
                "再等等", "先別碰", "抱著就好", "資料不足"]

# 收合群組標題(個股卡片內仍用上面的短詞)
_GROUP_TITLE = {
    "全部賣掉":   "全部賣掉",
    "賣一半":     "賣一半",
    "賣1/3":      "賣 1/3",
    "還能再買一點": "還能再買一點",
    "可以買":     "可以買",
    "先買一點":   "先買一點",
    "再等等":     "再等等",
    "先別碰":     "先別碰",
    "抱著就好":   "抱著就好",
    "資料不足":   "資料不足",
}


def _plain_order_key(advice: str) -> int:
    lab = _plain_action(advice)[1]
    return _PLAIN_ORDER.index(lab) if lab in _PLAIN_ORDER else 99


# ---------------------------------------------------------------------------
def _run_technical_inline(scope: str = "all") -> dict:
    """在 app 這台直接跑「更新股價走勢」(技術+重算+變化偵測),不送 GitHub。
    scope: 'positions'(只持股) / 'watchlist'(只追蹤) / 'all'。"""
    import asyncio
    try:
        from fugle_agent.tools import (organize_all_technical, _recompute_advice,
                                       detect_intraday_changes)
        asyncio.run(organize_all_technical.handler({"scope": scope}))
        _recompute_advice(scope)
        try:
            detect_intraday_changes(scope)
        except Exception:
            pass   # 變化偵測失敗不影響主更新
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@st.cache_data(ttl=90, show_spinner=False)
def _cached_tab(tab: str) -> list:
    """讀 Sheet 分頁(暫存 90 秒)→ 切 tab/互動時不用每次重抓,大幅減少 lag。
    按更新或🔁重新整理會清快取,所以拿得到最新。"""
    return sheets.fetch_tab(tab)


@st.cache_data(ttl=90, show_spinner=False)
def _cached_watchlist() -> list:
    return sheets.load_watchlist()


@st.cache_data(ttl=15, show_spinner=False)
def _prefetch_live_lights(symbols: tuple[str, ...]) -> dict:
    """一次把『此刻燈 / 今天燈』所需的即時資料抓好(並行 + 快取 15 秒)。
    回 {sym: {"now": {...}, "today": {...}}}。任何失敗都吞掉、回 ⚪ 資料不足,
    絕不讓整頁壞掉。快取 15 秒 → 你一打開/互動就是即時,又不會每次重畫都猛打 Fugle。"""
    from concurrent.futures import ThreadPoolExecutor
    from fugle_agent import intraday_lights as il

    def _one(sym: str) -> tuple[str, dict]:
        try:
            from fugle_agent.client import FugleClient
            c = FugleClient()                 # 每個工作緒自己一個 client → 真並行
            quote = c.quote(sym)
            series = il.series_from_candles(c.intraday_candles(sym))
            if len(series) < 2:               # 分鐘K拿不到 → 退逐筆
                series = il.series_from_ticks(c.intraday_ticks(sym, limit=120))
            return sym, {"now": il.now_light(series, quote),
                         "today": il.today_light(quote)}
        except Exception:
            return sym, {"now": {"light": "⚪ 資料不足", "reason": ""},
                         "today": {"light": "⚪ 資料不足", "reason": ""}}

    out: dict[str, dict] = {}
    syms = [s for s in symbols if s]
    if not syms:
        return out
    try:
        with ThreadPoolExecutor(max_workers=min(6, len(syms))) as pool:
            for sym, res in pool.map(_one, syms):
                out[sym] = res
    except Exception:
        pass
    return out


@st.cache_data(ttl=15, show_spinner=False)
def _prefetch_forecasts(items: tuple, is_holding: bool) -> dict:
    """一整頁的三盞預測 + 怎麼辦 + 現價/損益,即時並行算好(快取 15 秒)。
    items = ((代號, 股數, 總成本), ...)。任何失敗都回空 dict,不讓整頁壞。"""
    try:
        from fugle_agent import live_forecast
        return live_forecast.prefetch(list(items), is_holding=is_holding)
    except Exception:
        return {}


def _fc_get(r: dict) -> dict:
    sym = _g(r, "代號") or _g(r, "symbol")
    return (st.session_state.get("_forecasts") or {}).get(sym, {})


def _fc_group(action: str) -> tuple[int, str]:
    """依「怎麼辦」開頭的燈色分組(要注意的排最前)。"""
    a = (action or "").strip()
    if not a:
        return (3, "⚪ 資料不足")
    c = a[0]
    if c == "🔴":
        return (0, "🔴 要注意的")
    if c == "🟢":
        return (1, "🟢 可以動作的")
    if c == "🟡":
        return (2, "🟡 再等等的")
    return (3, "⚪ 資料不足")


def _fc_label(action: str) -> tuple[str, str]:
    """把「怎麼辦」拆成 (燈色 icon, 短標題),給卡片標題用。"""
    a = (action or "").split("　")[0].strip()   # 去掉大盤逆風那段
    if not a:
        return ("⚪", "資料不足")
    c = a[0]
    if c in "🟢🟡🔴⚪":
        short = a[1:].split("、")[0].split("（")[0].split("(")[0].strip()
        return (c, short or "—")
    return ("⚪", a.split("、")[0][:8])


@st.cache_data(ttl=25, show_spinner=False)
def _market_ctx_cached() -> dict:
    """大盤順逆風。快取 25 秒(< 橫幅自動刷新的 30 秒),
    讓每次自動刷新都拿到新的即時指數,又能讓同一秒的多次互動共用、不重打。"""
    try:
        from fugle_agent import market_context
        return market_context.get_market_context()
    except Exception:
        return {"light": "🟡 普通", "phase": "", "is_headwind": False,
                "reason": "大盤資料抓不到,當作普通", "updated": ""}


@st.cache_data(ttl=25, show_spinner=False)
def _us_market_cached() -> dict:
    """美股三大指數(yfinance,延遲約 15 分)。回 {items, date, light}。
    light 算法跟大盤盤前一樣:三指數平均 ≥+0.5%→偏強、≤-0.5%→偏弱、其餘普通。"""
    out: dict = {"items": {}, "date": "", "time": "", "light": "🟡 普通"}
    try:
        from fugle_agent import us_market
        asof = ""
        for zh, sym in (("費半", "^SOX"), ("標普", "^GSPC"), ("那斯達克", "^IXIC")):
            q = us_market.quote(sym)
            if isinstance(q, dict) and not q.get("error") and q.get("changePercent") is not None:
                out["items"][zh] = float(q["changePercent"])
                if not asof and q.get("asOf"):
                    asof = str(q["asOf"])[:10]
        out["date"] = asof
        if out["items"]:
            out["time"] = us_market.latest_index_time()    # 台北 到分(延遲約15分)
            avg = sum(out["items"].values()) / len(out["items"])
            out["light"] = "🟢 偏強" if avg >= 0.5 else ("🔴 偏弱" if avg <= -0.5 else "🟡 普通")
    except Exception:
        pass
    return out


def _render_us_line() -> None:
    """大盤下面再加一條美股,格式跟大盤橫幅一樣(燈號 + 漲跌 + 資料日期)。
    開著頁面每 30 秒一起刷;美股開盤(台北晚上)會跟著跳(延遲約 15 分,只到日)。"""
    us = _us_market_cached()
    items = us.get("items") or {}
    if not items:
        return
    light = us.get("light", "🟡 普通")
    if "🔴" in light:
        bg, bd = "rgba(226,75,74,.10)", "rgba(226,75,74,.40)"
    elif "🟢" in light:
        bg, bd = "rgba(99,153,34,.10)", "rgba(99,153,34,.40)"
    else:
        bg, bd = "rgba(127,127,127,.07)", "rgba(127,127,127,.25)"
    parts = []
    for zh, pct in items.items():
        col = "var(--color-text-success)" if pct >= 0 else "var(--color-text-danger)"
        parts.append(f'{zh} <span style="color:{col};font-weight:500">{pct:+.1f}%</span>')
    when = us.get("time") or us.get("date") or ""        # 優先到分,退而到日
    dtag = f"(資料 {when}・延遲約15分)" if when else "(延遲約15分)"
    st.markdown(
        f'<div style="background:{bg};border:0.5px solid {bd};border-radius:10px;'
        f'padding:8px 12px;margin-bottom:10px;font-size:13px">'
        f'🌎 <b>美股：{light}</b>　' + ' · '.join(parts) + f'　{dtag}</div>',
        unsafe_allow_html=True)


def _render_market_banner() -> bool:
    """畫大盤順逆風橫幅(全頁背景),回傳 is_headwind(逆風=True)。"""
    ctx = _market_ctx_cached()
    head = bool(ctx.get("is_headwind"))
    light = ctx.get("light", "🟡 普通")
    reason = ctx.get("reason", "")
    # 注意:大盤橫幅顯示的是「資料本身的時間」(已含在 reason 的 dtag 裡,到秒),
    # 不是系統時間,所以這裡不再附加「更新 剛剛」那種系統時間。
    if head:
        bg, bd = "rgba(226,75,74,.10)", "rgba(226,75,74,.40)"
    elif "🟢" in light:
        bg, bd = "rgba(99,153,34,.10)", "rgba(99,153,34,.40)"
    else:
        bg, bd = "rgba(127,127,127,.07)", "rgba(127,127,127,.25)"
    st.markdown(
        f'<div style="background:{bg};border:0.5px solid {bd};border-radius:10px;'
        f'padding:8px 12px;margin-bottom:10px;font-size:13px">'
        f'🌡️ <b>大盤：{light}</b>　{reason}</div>',
        unsafe_allow_html=True)
    return head


@st.fragment(run_every="30s")
def _market_banner_fragment() -> None:
    """只讓『大盤橫幅』這一塊每 30 秒自己重算重畫,其他區塊不跟著重跑
    (避免整頁刷新造成 lag)。順便把逆風狀態寫進 session_state 給買進煞車用。"""
    st.session_state["_mkt_headwind"] = _render_market_banner()
    _render_us_line()
    _render_forecast_freshness()


def _render_forecast_freshness() -> None:
    """三盞預測各自的「資料新鮮度」,一盞一行,排在大盤、美股下面。
    (三盞是即時算的,所以這裡標的是『它用的資料有多新』。)"""
    rows = [
        ("🔮", "下一小時", "即時(到秒)"),
        ("🌤️", "明天", "今天收盤 ＋ 隔夜美股(延遲約15分)"),
        ("📅", "三天後", "日線昨收 ＋ 外資(每日)"),
    ]
    html = ""
    for icon, name, fresh in rows:
        html += (
            '<div style="background:rgba(127,127,127,.07);border:0.5px solid '
            'rgba(127,127,127,.25);border-radius:10px;padding:6px 12px;margin-bottom:8px;'
            'font-size:13px">'
            f'{icon} <b>{name}</b>　<span style="color:#5F5E5A">{fresh}</span></div>')
    st.markdown(html, unsafe_allow_html=True)


def render(trigger_workflow, job_indicator, mark_job_started, cancel_all=None,
           workflow_running=None, cleanup_watchlist=None) -> None:
    st.markdown("""<style>
    /* 收緊整體上下間距,貼近 mock */
    section.main div[data-testid="stVerticalBlock"]{gap:.5rem}
    div[data-testid="stExpander"]{margin-bottom:7px}

    /* 切換鈕(我的持股 / 我的追蹤)→ 單純點選,字大一點、給點間距 */
    div[role="radiogroup"]{gap:22px;margin-bottom:10px}
    div[role="radiogroup"]>label p{font-size:16px!important;margin:0!important}

    /* 卡片 / 收合區(expander)→ 圓角細邊框、適當內距 */
    div[data-testid="stExpander"] details{border:0.5px solid rgba(127,127,127,.22)!important;
        border-radius:12px!important;background:rgba(127,127,127,.035);overflow:hidden}
    div[data-testid="stExpander"] summary{padding:11px 14px!important;font-size:14px!important}
    div[data-testid="stExpander"] summary:hover{background:rgba(127,127,127,.07)}

    /* 按鈕圓角 */
    div[data-testid="stButton"]>button{border-radius:10px}
    </style>""", unsafe_allow_html=True)

    # ── 持有/追蹤切換(標題由 app.py 顯示,這裡不重複)──
    mode = st.radio("檢視", ["我的持股", "我的追蹤"], horizontal=True,
                    label_visibility="collapsed", key="dash_mode")
    mode = "持有" if mode == "我的持股" else "追蹤"

    # ── 🌡️ 大盤順逆風(背景,全頁共用;當下算、不存)──
    # 每次打開 → 短快取(25 秒)保證是最新即時指數;開著時 → 每 30 秒自動刷新這條。
    if "_mkt_headwind" not in st.session_state:
        st.session_state["_mkt_headwind"] = False
    _market_banner_fragment()


    # 跑中狀態(鎖按鈕用)— 每次都問 GitHub 4 次很慢,改成「最多 20 秒問一次」,
    # 中間用上次的結果(切 tab/互動就不會卡在等 GitHub 回應)
    import time as _time
    _wr = st.session_state.get("_wr_cache")
    if workflow_running and (not _wr or _time.time() - _wr.get("ts", 0) > 20):
        _wr = {"ts": _time.time(),
               "full":  workflow_running("full_update.yml", "all"),
               "intra": workflow_running("intraday_update.yml", "all"),
               "pos":   workflow_running("full_update.yml", "positions_new"),
               "wl":    workflow_running("full_update.yml", "watchlist_new")}
        st.session_state["_wr_cache"] = _wr
    elif not workflow_running:
        _wr = {"full": False, "intra": False, "pos": False, "wl": False}
    full_running, intra_running = _wr["full"], _wr["intra"]
    pos_running, wl_running = _wr["pos"], _wr["wl"]
    any_running = full_running or intra_running or pos_running or wl_running

    # ── 主畫面:每檔股票一張卡,要動手的在最上面 ──
    if mode == "持有":
        _render_holdings()
    else:
        _render_watchlist()

    # ── 👀 今天要注意的(收合)──
    _ncnt, _cbody = _changes_html()
    with st.expander(f"👀 今天要注意的（{_ncnt}）" if _ncnt else "👀 今天要注意的"):
        st.markdown(_cbody, unsafe_allow_html=True)

    # ── 💰 我現在賺多少(只有持股,收合)──
    if mode == "持有":
        with st.expander("💰 我現在賺多少"):
            _render_totals()

    # ── ⚙️ 更新與設定(按鈕都收這)──
    def _after(r, key, secs, ok_msg, act_mode=None, act_name=None):
        if r.get("ok"):
            mark_job_started(key, secs)
            if act_mode and act_name:
                _mark_action(act_mode, act_name)   # 記在那一頁的進度(含開始時間)
            st.rerun()
        else:
            st.error(f"❌ {r.get('error')}")

    with st.expander("⚙️ 更新與設定",
                     expanded=bool(st.session_state.get("_settings_open"))):
        if any_running:
            st.caption("⏳ 有更新正在跑…跑完前按鈕會鎖住")
        # 主要動作:只顯示「當前這一頁」相關的(持股↔我剛買賣股票、追蹤↔我剛加追蹤)
        if mode == "持有":
            st.caption("加完股票交易按這個(免費)")
            if st.button("＋ 我剛買賣股票", use_container_width=True, disabled=any_running,
                         help="用股票交易重算持股、賺賠;只幫全新股票補公司面"):
                _after(trigger_workflow("full_update.yml", inputs={"scope": "positions_new"}),
                       "full_update", 900, "已開始更新持股(背景跑)",
                       act_mode="持有", act_name="重算持股")
        else:
            st.caption("加完追蹤按這個(免費)")
            if st.button("＋ 我剛加追蹤", use_container_width=True, disabled=any_running,
                         help="只分析追蹤清單裡新加的那幾檔"):
                _after(trigger_workflow("full_update.yml", inputs={"scope": "watchlist_new"}),
                       "full_update", 900, "已開始分析新追蹤(背景跑)",
                       act_mode="追蹤", act_name="分析新追蹤")

        st.caption("想立刻看最新股價(免費、當場跑、幾秒~十幾秒)")
        if st.button("⚡ 更新最新股價走勢", use_container_width=True, disabled=any_running,
                     help="直接在這台抓最新股價、重算走勢和「該做啥」(免費,不含公司面)"):
            # 第一段:只先標記「正在進行中」(存在「當前這一頁」的 key)+ 立刻重畫,
            # 真正的計算放到 render 最後才跑,確保「正在進行中」先畫在這一頁的「資料時間」下
            _t0 = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            _mark_action(mode, "更新股價走勢")
            st.session_state["_pending_start"] = _t0.isoformat()
            st.session_state["_pending_mode"] = mode
            st.rerun()

        st.caption("想連公司基本面重查一遍(花一點錢,一週一次就好)")
        if st.button("🔍 重查公司基本面", use_container_width=True, disabled=any_running,
                     help="重查公司估值/配息/營收/法人/新聞(會花一點錢)"):
            _after(trigger_workflow("full_update.yml", inputs={"scope": "all"}),
                   "full_update", 900, "已開始重查公司基本面(背景跑)",
                   act_mode=mode, act_name="重查公司基本面")

        t1, t2 = st.columns(2)
        with t1:
            if st.button("🔁 重新整理", use_container_width=True):
                st.cache_data.clear()
                st.rerun()
        with t2:
            if cancel_all and st.button("🛑 終止跑中", use_container_width=True):
                res = cancel_all()
                if res.get("ok"):
                    st.success(f"✅ 已取消 {res.get('n_cancelled', 0)} 個")
                else:
                    st.error(f"❌ {res.get('error')}")
                st.rerun()

    # 第二段:真正執行「更新股價走勢」— 放在 render 最後,
    # 此時這一頁「資料時間」下的『正在進行中』已經畫出來給使用者看了,才開始算。
    # 只更新「按下去那一頁」的範圍(持有→positions、追蹤→watchlist),狀態也只寫那一頁。
    _ps = st.session_state.pop("_pending_start", None)
    _pm = st.session_state.pop("_pending_mode", None)
    if _ps and _pm:
        _scope = "positions" if _pm == "持有" else "watchlist"
        r = _run_technical_inline(_scope)
        if r.get("ok"):
            _mark_action_done(_pm, ok=True)
            st.cache_data.clear()
        else:
            _mark_action_done(_pm, ok=False, err=str(r.get("error", "")))
        st.rerun()


def _last_update_caption(rows: list[dict], mode: str = "持有") -> None:
    # 三盞預測是「打開頁面當下即時算」的 → 沒有「技術資料時間」可顯示;
    # 這裡只顯示「公司簡介/估值/新聞」那批 AI 資料是哪天刷的。
    fund = max((_g(r, "公司更新時間", "基本面資料時間") for r in rows), default="")
    ft, fs = _rel_time(fund, 60 * 24 * 5)
    warn = "　⚠️可跑深度分析刷新" if fs else ""
    # 跟大盤、美股一樣做成一條橫幅排在上面:公司資料的更新時間。
    # (三盞預測是「打開頁面即時算」,本身沒有「幾分前」,所以不列時間。)
    st.markdown(
        '<div style="background:rgba(127,127,127,.07);border:0.5px solid rgba(127,127,127,.25);'
        'border-radius:10px;padding:8px 12px;margin-bottom:10px;font-size:13px">'
        f'🏢 <b>公司資料：</b>{ft}{warn}　'
        '<span style="color:#5F5E5A;font-size:12px">'
        '(估值/配息/營收/新聞/簡介,每週日自動刷;三盞預測為即時算)</span></div>',
        unsafe_allow_html=True)
    # 按鈕進度(每一頁各自獨立,跑完/重整都不丟)
    _render_tab_status(mode)


def _now_tw_str() -> str:
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def _mark_action(mode: str, name: str) -> None:
    """按鈕按下去 → 記在「這一頁」的進度(含開始時間,到秒)。"""
    st.session_state[f"_act_{mode}"] = {"name": name, "started": _now_tw_str(),
                                        "state": "running", "finished": "", "err": ""}


def _mark_action_done(mode: str, ok: bool = True, err: str = "") -> None:
    act = st.session_state.get(f"_act_{mode}")
    if not act:
        return
    act["state"] = "done" if ok else "fail"
    act["finished"] = _now_tw_str()
    act["err"] = err
    st.session_state[f"_act_{mode}"] = act


def _render_tab_status(mode: str) -> None:
    """顯示「這一頁」最近一次按鈕的進度:⏳ 正在 …(開始於 時間)/ ✅ … 完成於 時間。
    進行中與完成都靠 GitHub 真實狀態判斷,所以重新整理頁面也不會丟、兩頁互不影響。"""
    act = st.session_state.get(f"_act_{mode}")
    wr = st.session_state.get("_wr_cache") or {}
    tab_running = bool(wr.get("pos" if mode == "持有" else "wl")) or bool(wr.get("full"))

    # 沒有按過紀錄、但 GitHub 顯示這頁有工作在跑(可能剛 F5 過)→ 仍顯示進行中
    if not act:
        if tab_running:
            st.info("⏳ 這一頁有更新正在跑…(重整也擋得住)")
        return

    if act.get("state") == "running":
        buffering = False           # 剛按下去 30 秒內:GitHub 可能還沒登記,先當進行中
        try:
            t0 = datetime.datetime.strptime(act["started"], "%Y-%m-%d %H:%M:%S")
            buffering = ((datetime.datetime.utcnow() + datetime.timedelta(hours=8) - t0)
                         .total_seconds() < 30)
        except Exception:
            pass
        if tab_running or buffering:
            st.info(f"⏳ 正在{act['name']}…（開始於 {act['started']}）")
        else:
            _mark_action_done(mode, ok=True)
            act = st.session_state[f"_act_{mode}"]
            st.success(f"✅ {act['name']} 完成於 {act['finished']}")
    elif act.get("state") == "fail":
        st.error(f"❌ {act['name']} 失敗於 {act.get('finished', '')}：{act.get('err', '')}")
    else:
        st.success(f"✅ {act['name']} 完成於 {act.get('finished', '')}")


def _render_holdings() -> None:
    tab = os.getenv("PORTFOLIO_POSITIONS_TAB", sheets.DEFAULT_POSITIONS_TAB)
    try:
        rows = [r for r in (_cached_tab(tab) or [])
                if not r.get("_error") and _g(r, "代號", "symbol")]
    except Exception as e:
        st.error(f"讀股票部位失敗:{e}")
        return
    if not rows:
        st.info("還沒有持股 — 在「股票交易」加交易,再到 ⚙️ 按「我剛買賣股票」。")
        return
    _last_update_caption(rows, "持有")
    _render_stock_list(rows, _holding_card, act_top=False, is_holding=True)


def _render_watchlist() -> None:
    try:
        rows = [r for r in (_cached_watchlist() or [])
                if not r.get("_error") and _g(r, "代號", "symbol")]
    except Exception as e:
        st.error(f"讀追蹤清單失敗:{e}")
        return
    if not rows:
        st.info("還沒有追蹤 — 在「追蹤清單」加代號,再到 ⚙️ 按「我剛加追蹤」。")
        return
    _last_update_caption(rows, "追蹤")
    _render_stock_list(rows, _watch_card, act_top=False, is_holding=False)


def _render_totals() -> None:
    tab = os.getenv("PORTFOLIO_POSITIONS_TAB", sheets.DEFAULT_POSITIONS_TAB)
    try:
        rows = [r for r in (_cached_tab(tab) or []) if not r.get("_error")]
    except Exception:
        rows = []
    cost = sum(_num(_g(r, "總成本")) or 0 for r in rows)
    # 損益改即時算(不存 Sheet):用同一份快取的預測結果加總
    items = tuple(
        (str(_g(r, "代號") or _g(r, "symbol")),
         int(_num(_g(r, "股數")) or 0),
         float(_num(_g(r, "總成本")) or 0.0))
        for r in rows if (_g(r, "代號") or _g(r, "symbol"))
    )
    fcs = _prefetch_forecasts(items, True)
    pnl = sum((v.get("pnl") or {}).get("損益", 0) or 0 for v in fcs.values())
    realized = _realized_total()
    m1, m2, m3 = st.columns(3)
    m1.metric("總成本", f"{cost:,.0f}")
    m2.metric("未實現損益(還沒賣)", f"{pnl:+,.0f}")
    m3.metric("已實現損益(賣掉的)", f"{realized:+,.0f}")


def _render_stock_list(rows: list[dict], card_fn, act_top: bool = True,
                       is_holding: bool = True) -> None:
    """即時算好整頁的三盞預測 + 怎麼辦,依「怎麼辦」的燈色分組(要注意的排最前)。"""
    # 一次並行算好整頁(快取 15 秒);卡片只讀結果、不各自打網路。
    items = tuple(
        (str(_g(r, "代號") or _g(r, "symbol")),
         int(_num(_g(r, "股數")) or 0),
         float(_num(_g(r, "總成本")) or 0.0))
        for r in rows if (_g(r, "代號") or _g(r, "symbol"))
    )
    try:
        st.session_state["_forecasts"] = _prefetch_forecasts(items, is_holding)
    except Exception:
        st.session_state["_forecasts"] = {}

    def _act(r: dict) -> str:
        return _fc_get(r).get("action") or _g(r, "我該做啥", "綜合建議") or ""

    def _hdr(a: str) -> str:        # 分組標題 = 「怎麼辦」本身(去掉大盤逆風那段)
        return (a or "").split("　")[0].strip() or "⚪ 資料不足"

    def _pri(a: str) -> int:        # 排序:🔴 要動的排最前 → 🟢 → 🟡 → ⚪
        return {"🔴": 0, "🟢": 1, "🟡": 2}.get((a or "").strip()[:1], 3)

    rows = sorted(rows, key=lambda r: (_pri(_act(r)), _hdr(_act(r))))
    order, groups = [], {}
    for r in rows:
        lab = _hdr(_act(r))
        if lab not in groups:
            groups[lab] = []
            order.append(lab)
        groups[lab].append(r)
    for lab in order:
        with st.expander(f"{lab}（{len(groups[lab])} 檔）", expanded=lab.startswith("🔴")):
            st.markdown('<div class="gyh-card">', unsafe_allow_html=True)
            for r in groups[lab]:
                card_fn(r)
            st.markdown('</div>', unsafe_allow_html=True)


def _holding_card(r: dict) -> None:
    code = _g(r, "代號", "symbol")
    name = _g(r, "名稱", "name")
    fc = _fc_get(r)
    action = fc.get("action") or _g(r, "我該做啥", "綜合建議")
    pnl = fc.get("pnl") or {}
    pct = pnl.get("損益%")
    pct_txt = f"　{'賺' if pct >= 0 else '賠'} {abs(pct):.1f}%" if pct is not None else ""
    mkt = fc.get("market") or ""
    code_txt = f"{code}·{mkt}" if mkt else code
    label = f"{code_txt} {name}{pct_txt}"
    with st.expander(label):
        _detail_common(r, action)


def _watch_card(r: dict) -> None:
    code = _g(r, "代號", "symbol")
    name = _g(r, "名稱", "name")
    reason = _g(r, "追蹤理由")
    fc = _fc_get(r)
    action = fc.get("action") or _g(r, "我該做啥", "綜合建議")
    mkt = fc.get("market") or ""
    code_txt = f"{code}·{mkt}" if mkt else code
    tag = f"（{reason}）" if reason else ""
    label = f"{code_txt} {name}{tag}"
    with st.expander(label):
        _detail_common(r, action)


def _fnum(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _risk_line(r: dict, is_holding: bool) -> str:
    """停損點 + 賺賠比(現算,不存欄位)。持股=賺賠比;追蹤=進場停損參考。"""
    mut = "color:#5F5E5A"
    cur = _fnum(_g(r, "現價"))
    stop = _fnum(_g(r, "停損價"))
    if not cur or not stop or stop <= 0:
        return ""
    down = (cur - stop) / cur * 100      # 離停損%(正=還有空間)
    # 找「下一個目標」:持股用淨賺價、追蹤用離20天高(沒有就略過賺賠比)
    target = None
    for col in ("淨賺5%價", "淨賺10%價", "淨賺15%價", "淨賺20%價"):
        p = _fnum(_g(r, col))
        if p and p > cur:
            target = p
            break
    lines = [f"跌破 10 日線(約 {stop:.1f} 元)就走，離停損 {down:+.1f}%"]
    if target and down > 0:
        up = (target - cur) / cur * 100
        ratio = up / down if down else 0
        verdict = ("划算" if ratio >= 1.5 else ("還好" if ratio >= 1 else "偏不划算、別貪"))
        lines.append(f"再漲 +{up:.1f}% 到下一個目標　賺賠比約 {up:.0f}:{down:.0f} → {verdict}")
    head = "💰 <b>賺賠比</b>" if is_holding else "💰 <b>進場參考</b>"
    body = "<br>".join(lines)
    return (f'<div style="margin-top:8px">{head}'
            f'<br><span style="{mut};font-size:13px">{body}</span></div>')


def _detail_common(r: dict, action: str) -> None:
    """卡片內容(全部即時算):現價/損益 → 三盞預測 → 怎麼辦 → 關於這檔。"""
    mut = "color:#5F5E5A"
    fc = _fc_get(r)

    # 1) 現價 / 損益(即時,不存 Sheet)
    price = fc.get("price")
    pnl = fc.get("pnl") or {}
    dt = fc.get("data_time")
    mkt = fc.get("market") or ""
    if price is not None:
        line = f'💲 <b>現價</b>　{price:g}'
        if mkt:
            line += f'　<span style="{mut};font-size:12px">{mkt}</span>'
        if pnl.get("損益%") is not None:
            v = pnl["損益%"]
            col = "var(--color-text-success)" if v >= 0 else "var(--color-text-danger)"
            line += (f'　<span style="color:{col};font-weight:500">'
                     f'{"賺" if v >= 0 else "賠"} {abs(v):.1f}%　{pnl.get("損益", 0):+,.0f}</span>')
        st.markdown(line + (f'<br><span style="{mut};font-size:12px">資料時間 {dt}</span>'
                            if dt else ""), unsafe_allow_html=True)

    # 2) 三盞預測:⚡下一小時 → 📊明天 → 📅三天後
    def _fline(icon: str, name: str, f: dict) -> str:
        lean = f.get("lean", "")
        if not lean:
            return ""
        conf = f.get("conf", "")
        reason = f.get("reason", "")
        conf_txt = f'　<span style="{mut};font-size:12px">信心{conf}</span>' if conf else ""
        return (f'<div style="margin-top:7px">{icon} <b>{name}</b>　{lean}{conf_txt}'
                + (f'<br><span style="{mut};font-size:13px">{reason}</span>' if reason else "")
                + '</div>')

    st.markdown(
        _fline("⚡", "下一小時", fc.get("next_hour", {}))
        + _fline("📊", "明天", fc.get("tomorrow", {}))
        + _fline("📅", "三天後", fc.get("three_day", {})),
        unsafe_allow_html=True)

    # 3) 怎麼辦(用三盞預測算好的,已含大盤逆風提醒)
    act = action or fc.get("action") or ""
    if act:
        st.markdown('<div style="margin-top:10px;border-top:0.5px solid rgba(127,127,127,.2);'
                    f'padding-top:8px">👉 <b>怎麼辦</b>　{act}</div>', unsafe_allow_html=True)

    # 4) 關於這檔(備註區):體質(最上面、最顯眼)→ 公司在幹嘛 → 新聞 → 估值/配息/營收
    health = _g(r, "公司體質")
    about = _g(r, "公司簡介")
    items = [
        ("新聞", _g(r, "新聞", "近期新聞重點")),
        ("估值", _g(r, "估值")),
        ("配息", _g(r, "配息")),
        ("營收", _g(r, "營收動能")),
    ]
    body = "".join(
        f'<div style="display:flex;gap:8px;font-size:13px;padding:2px 0">'
        f'<span style="{mut};min-width:34px">{k}</span><span>{v}</span></div>'
        for k, v in items if v)
    if health or about or body:
        st.markdown('<div style="margin-top:12px">📋 <b>關於這檔</b></div>', unsafe_allow_html=True)
        if health:
            st.markdown(f'<div style="font-size:15px;font-weight:500;margin:4px 0 2px">{health}</div>',
                        unsafe_allow_html=True)
        if about:
            st.markdown(f'<div style="{mut};font-size:13px;margin-bottom:4px">🏢 {about}</div>',
                        unsafe_allow_html=True)
        if body:
            st.markdown(body, unsafe_allow_html=True)
        ft, fs = _rel_time(_g(r, "公司更新時間", "基本面資料時間"), 60 * 24 * 5)
        st.caption(f"公司資料 {ft}{' ⚠️舊' if fs else ''}")


def _realized_total() -> float:
    try:
        total = 0.0
        for r in (_cached_tab("實際損益") or []):
            if r.get("_error"):
                continue
            v = _num(_g(r, "實際損益", "realized_pnl"))
            if v is not None:
                total += v
        return total
    except Exception:
        return 0.0
