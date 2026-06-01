# 📅 ★最新版★ 上傳於 2026-06-02 (原最後更新 2026-05-29)(持股頁全收合,該賣的也收成群組)
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


# 動作詞 → (icon, 白話標籤, 是否「要動手」)。要動手的排最上面,其他收合分組。
def _plain_action(advice: str) -> tuple[str, str, bool]:
    a = _action_short(advice)
    # 賣:一律全賣(不分批),減碼類也歸這
    if (a.startswith("趕快賣") or a.startswith("停損") or a.startswith("該賣")
            or a.startswith("賣一批") or a.startswith("先減碼") or a.startswith("偏減碼")):
        return ("🛑", "該賣了", True)
    # 加碼:收成一組(不急,點開看)
    if a.startswith("可以加碼") or a.startswith("抱緊加碼"):
        return ("💪", "可以加碼", False)
    # 買(追蹤)
    if a.startswith("趕快買"):
        return ("🚀", "可以買", True)
    if a.startswith("可以買"):
        return ("🟢", "可以慢慢買", True)
    # 不用動
    if a.startswith("盯緊") or a.startswith("留意"):
        return ("👀", "盯緊一點", False)
    if a.startswith("續抱") or a.startswith("抱"):
        return ("🤲", "抱著就好", False)
    if a.startswith("再等等"):
        return ("⏸️", "再等等", False)
    if a.startswith("先別") or a.startswith("不要"):
        return ("🚫", "先別碰", False)
    # 其餘(⏳ 等分析、空白…)
    return ("⚪", "資料不足", False)


# 由上到下的優先序(要動手的在前)
_PLAIN_ORDER = ["該賣了", "可以買", "可以慢慢買", "可以加碼",
                "盯緊一點", "抱著就好", "再等等", "先別碰", "資料不足"]

# 收合群組標題用的白話講法(個股卡片內仍用上面的短詞)
_GROUP_TITLE = {
    "該賣了":   "今天該賣的",
    "可以加碼": "還能再買一點的",
    "盯緊一點": "要盯緊的",
    "抱著就好": "抱著就好的",
    "可以買":   "可以買的",
    "可以慢慢買": "可以慢慢買的",
    "再等等":   "再等等的",
    "先別碰":   "先別碰的",
    "資料不足": "資料不足",
}


def _plain_order_key(advice: str) -> int:
    lab = _plain_action(advice)[1]
    return _PLAIN_ORDER.index(lab) if lab in _PLAIN_ORDER else 99


# ---------------------------------------------------------------------------
def render(trigger_workflow, job_indicator, mark_job_started, cancel_all=None,
           workflow_running=None, cleanup_watchlist=None) -> None:
    st.markdown("""<style>
    /* 收緊整體上下間距,貼近 mock */
    section.main div[data-testid="stVerticalBlock"]{gap:.5rem}
    div[data-testid="stExpander"]{margin-bottom:7px}

    /* 切換鈕(我的持股 / 我在追蹤)→ 膠囊分段樣式 */
    div[role="radiogroup"]{gap:8px;margin-bottom:6px}
    div[role="radiogroup"]>label{flex:1;display:flex;justify-content:center;align-items:center;
        padding:9px 0;border:0.5px solid rgba(127,127,127,.25);border-radius:10px;margin:0!important;cursor:pointer}
    div[role="radiogroup"]>label>div:first-child{display:none}            /* 藏掉圓圈 */
    div[role="radiogroup"]>label:has(input:checked){background:rgba(127,127,127,.14);
        border-color:rgba(127,127,127,.6);font-weight:600}

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

    # 跑中狀態(鎖按鈕用)
    if workflow_running:
        full_running = workflow_running("full_update.yml", "all")
        intra_running = workflow_running("intraday_update.yml", "all")
        pos_running = workflow_running("full_update.yml", "positions_new")
        wl_running = workflow_running("full_update.yml", "watchlist_new")
    else:
        full_running = intra_running = pos_running = wl_running = False
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
    def _after(r, key, secs, ok_msg):
        if r.get("ok"):
            mark_job_started(key, secs)
            st.success(f"✅ {ok_msg}")
            st.rerun()
        else:
            st.error(f"❌ {r.get('error')}")

    with st.expander("⚙️ 更新與設定"):
        if any_running:
            st.caption("⏳ 有更新正在跑…跑完前按鈕會鎖住")
        # 主要動作:只顯示「當前這一頁」相關的(持股↔我剛買賣股票、追蹤↔我剛加追蹤)
        if mode == "持有":
            st.caption("加完股票交易按這個(免費)")
            if st.button("＋ 我剛買賣股票", use_container_width=True, disabled=any_running,
                         help="用股票交易重算持股、賺賠;只幫全新股票補公司面"):
                _after(trigger_workflow("full_update.yml", inputs={"scope": "positions_new"}),
                       "full_update", 900, "已開始更新持股(背景跑)")
        else:
            st.caption("加完追蹤按這個(免費)")
            if st.button("＋ 我剛加追蹤", use_container_width=True, disabled=any_running,
                         help="只分析追蹤清單裡新加的那幾檔"):
                _after(trigger_workflow("full_update.yml", inputs={"scope": "watchlist_new"}),
                       "full_update", 900, "已開始分析新追蹤(背景跑)")

        st.caption("想立刻看最新股價(免費)")
        if st.button("⚡ 更新最新股價走勢", use_container_width=True, disabled=any_running,
                     help="抓最新股價、重算走勢和「該做啥」(免費,不含公司面)"):
            _after(trigger_workflow("intraday_update.yml", inputs={"scope": "all"}),
                   "intraday_update", 180, "已開始更新股價走勢(背景跑)")

        st.caption("想連公司基本面重查一遍(花一點錢,一週一次就好)")
        if st.button("🔍 重查公司基本面", use_container_width=True, disabled=any_running,
                     help="重查公司估值/配息/營收/法人/新聞(會花一點錢)"):
            _after(trigger_workflow("full_update.yml", inputs={"scope": "all"}),
                   "full_update", 900, "已開始重查公司基本面(背景跑)")

        _repo = os.getenv("GITHUB_REPO", "kiddnumber5ykao/fugle-agent")
        st.markdown(
            f'<a href="https://github.com/{_repo}/actions" target="_blank" '
            f'style="display:block;text-align:center;padding:8px;border:0.5px solid '
            f'rgba(127,127,127,.3);border-radius:8px;text-decoration:none;margin:8px 0 4px">'
            f'📊 看更新狀況</a>', unsafe_allow_html=True)
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


def _last_update_caption(rows: list[dict]) -> None:
    # 顯示「資料時間」(資料本身是哪天的),不是「整理時間」(我幾點跑分析)
    tech = max((_g(r, "技術資料時間") for r in rows), default="")
    fund = max((_g(r, "基本面資料時間") for r in rows), default="")
    tt, ts = _rel_time(tech, 60 * 24 * 2)      # 技術資料 > 2 天才提醒
    ft, fs = _rel_time(fund, 60 * 24 * 5)       # 基本面資料 > 5 天才提醒
    warn = ""
    if ts:
        warn += "　⚠️ 技術資料有點舊"
    st.caption(f"資料時間 — 技術 {tt} · 基本面 {ft}{warn}")


def _render_holdings() -> None:
    tab = os.getenv("PORTFOLIO_POSITIONS_TAB", sheets.DEFAULT_POSITIONS_TAB)
    try:
        rows = [r for r in (sheets.fetch_tab(tab) or [])
                if not r.get("_error") and _g(r, "代號", "symbol")]
    except Exception as e:
        st.error(f"讀股票部位失敗:{e}")
        return
    if not rows:
        st.info("還沒有持股 — 在「股票交易」加交易,再到 ⚙️ 按「我剛買賣股票」。")
        return
    _last_update_caption(rows)
    _render_stock_list(rows, _holding_card, act_top=False)


def _render_watchlist() -> None:
    try:
        rows = [r for r in (sheets.load_watchlist() or [])
                if not r.get("_error") and _g(r, "代號", "symbol")]
    except Exception as e:
        st.error(f"讀追蹤清單失敗:{e}")
        return
    if not rows:
        st.info("還沒有追蹤 — 在「追蹤清單」加代號,再到 ⚙️ 按「我剛加追蹤」。")
        return
    _last_update_caption(rows)
    _render_stock_list(rows, _watch_card, act_top=False)


def _render_totals() -> None:
    tab = os.getenv("PORTFOLIO_POSITIONS_TAB", sheets.DEFAULT_POSITIONS_TAB)
    try:
        rows = [r for r in (sheets.fetch_tab(tab) or []) if not r.get("_error")]
    except Exception:
        rows = []
    cost = sum(_num(_g(r, "總成本")) or 0 for r in rows)
    pnl = sum(_num(_g(r, "損益")) or 0 for r in rows)
    realized = _realized_total()
    m1, m2, m3 = st.columns(3)
    m1.metric("總成本", f"{cost:,.0f}")
    m2.metric("未實現損益(還沒賣)", f"{pnl:+,.0f}")
    m3.metric("已實現損益(賣掉的)", f"{realized:+,.0f}")


def _render_stock_list(rows: list[dict], card_fn, act_top: bool = True) -> None:
    """act_top=True(持股):要動手的排最上面直接展,其餘收合分組。
    act_top=False(追蹤):全部依動作收成一組組,點開才看(連可以買也收起來)。"""
    rows.sort(key=lambda r: _plain_order_key(_g(r, "我該做啥", "綜合建議")))
    if act_top:
        act = [r for r in rows if _plain_action(_g(r, "我該做啥", "綜合建議"))[2]]
        rest = [r for r in rows if not _plain_action(_g(r, "我該做啥", "綜合建議"))[2]]
        st.markdown('<div class="gyh-card">', unsafe_allow_html=True)
        if act:
            for r in act:
                card_fn(r)
        else:
            st.success("今天沒什麼要動手的,放著就好 😌")
        st.markdown('</div>', unsafe_allow_html=True)
    else:
        rest = rows

    # 其他依動作詞分組,各自收合(平常不展開)
    order, groups = [], {}
    for r in rest:
        lab = _plain_action(_g(r, "我該做啥", "綜合建議"))[1]
        if lab not in groups:
            groups[lab] = []
            order.append(lab)
    for r in rest:
        groups[_plain_action(_g(r, "我該做啥", "綜合建議"))[1]].append(r)
    for lab in order:
        icon = _plain_action(_g(groups[lab][0], "我該做啥", "綜合建議"))[0]
        title = _GROUP_TITLE.get(lab, f"{lab}的")
        with st.expander(f"{icon} {title}（{len(groups[lab])} 檔）"):
            st.markdown('<div class="gyh-card">', unsafe_allow_html=True)
            for r in groups[lab]:
                card_fn(r)
            st.markdown('</div>', unsafe_allow_html=True)


def _holding_card(r: dict) -> None:
    code = _g(r, "代號", "symbol")
    name = _g(r, "名稱", "name")
    adv = _g(r, "我該做啥", "綜合建議")
    icon, label_w, _ = _plain_action(adv)
    pnl_pct = _num(_g(r, "損益%"))
    pct_txt = f"　{'賺' if pnl_pct >= 0 else '賠'} {abs(pnl_pct):.1f}%" if pnl_pct is not None else ""
    label = f"{icon} {label_w}　{code} {name}{pct_txt}"
    with st.expander(label):
        _detail_common(r, adv)
        # 目標賣價
        cur = _num(_g(r, "現價"))
        targets = [("回本", _num(_g(r, "回本價"))),
                   ("+5%", _num(_g(r, "淨賺5%價"))),
                   ("+10%", _num(_g(r, "淨賺10%價"))),
                   ("+15%", _num(_g(r, "淨賺15%價"))),
                   ("+20%", _num(_g(r, "淨賺20%價")))]
        nearest = None
        if cur is not None:
            cand = [(abs(p - cur), i) for i, (_, p) in enumerate(targets) if p is not None]
            if cand:
                nearest = min(cand)[1]
        cells = ""
        for i, (lab, p) in enumerate(targets):
            if p is None:
                continue
            style = ("outline:2px solid #378ADD;background:#E6F1FB;" if i == nearest else "")
            cells += (f'<div style="flex:1;text-align:center;padding:6px 2px;border:0.5px solid '
                      f'rgba(127,127,127,.2);border-radius:8px;font-size:12px;{style}">'
                      f'<div style="color:#5F5E5A;font-size:11px">{lab}</div>{p:g}</div>')
        if cells:
            st.markdown('<div style="margin-top:6px"><b style="font-size:13px">目標賣價</b>'
                        f'（最近現價的框起來)</div><div style="display:flex;gap:4px;margin-top:4px">{cells}</div>',
                        unsafe_allow_html=True)


def _watch_card(r: dict) -> None:
    code = _g(r, "代號", "symbol")
    name = _g(r, "名稱", "name")
    reason = _g(r, "追蹤理由")
    adv = _g(r, "我該做啥", "綜合建議")
    icon, label_w, _ = _plain_action(adv)
    tag = f"（{reason}）" if reason else ""
    label = f"{icon} {label_w}　{code} {name}{tag}"
    with st.expander(label):
        _detail_common(r, adv)


def _detail_common(r: dict, adv: str) -> None:
    """順序:損益 → 動能(+白話原因) → 基本面(+白話原因) → 我該做啥(最後)。"""
    mut = "color:#5F5E5A"
    # 1) 賺賠(持有才有)
    pnl_pct = _g(r, "損益%")
    if pnl_pct:
        pnl = _g(r, "損益")
        word = ""
        try:
            v = float(pnl_pct)
            col = "var(--color-text-success)" if v >= 0 else "var(--color-text-danger)"
            word = "賺 " if v >= 0 else "賠 "
        except ValueError:
            col = "inherit"
        st.markdown(f'💰 <b>賺賠</b>　<span style="color:{col};font-weight:500">{word}{pnl_pct}%'
                    f'{("  " + pnl) if pnl else ""}</span>', unsafe_allow_html=True)
    # 2) 最近走勢(白話)
    short = _g(r, "短線燈號")
    mom = _g(r, "動能原因")
    st.markdown(f'<div style="margin-top:8px">📈 <b>最近走勢</b>　{short}'
                + (f'<br><span style="{mut};font-size:13px">{mom}</span>' if mom else "")
                + '</div>', unsafe_allow_html=True)
    # 3) 基本面 — 拆成「公司面」+「籌碼面」兩塊,各自一個燈 + 白話原因
    ft, fs = _rel_time(_g(r, "基本面資料時間"), 60 * 24 * 5)

    def _fund_block(icon: str, title: str, light: str, items: list[tuple[str, str]]) -> None:
        lines = [(k, _g(r, k)) for k in items]
        lines = [(k, v) for k, v in lines if v]
        body = "".join(f'<div style="display:flex;justify-content:space-between;font-size:13px;'
                       f'padding:2px 0"><span style="{mut}">{k}</span><span>{v}</span></div>'
                       for k, v in lines)
        st.markdown(f'<div style="margin-top:10px">{icon} <b>{title}</b> '
                    f'{_fund_light_label(light)}</div>' + body, unsafe_allow_html=True)

    _fund_block("🏢", "這家公司", _g(r, "公司面燈號"),
                ["估值", "配息", "營收動能"])
    _fund_block("🐳", "大戶(法人)", _g(r, "籌碼面燈號"),
                ["法人籌碼", "近期新聞重點"])
    st.caption(f"公司資料 {ft}{' ⚠️舊' if fs else ''}")
    # 4) 怎麼辦(最後)
    reason = _action_reason(adv)
    st.markdown('<div style="margin-top:10px;border-top:0.5px solid rgba(127,127,127,.2);'
                'padding-top:8px">👉 <b>怎麼辦</b>　'
                + _pill(_plain_action(adv)[1], _pill_kind(adv))
                + (f'<br><span style="{mut};font-size:13px">{reason}</span>' if reason else "")
                + '</div>', unsafe_allow_html=True)
    # 本次花費
    cost = _g(r, "本次花費")
    if cost:
        st.caption(f"💰 本檔基本面花費 {cost}(估)")


def _realized_total() -> float:
    try:
        total = 0.0
        for r in (sheets.fetch_tab("實際損益") or []):
            if r.get("_error"):
                continue
            v = _num(_g(r, "實際損益", "realized_pnl"))
            if v is not None:
                total += v
        return total
    except Exception:
        return 0.0
