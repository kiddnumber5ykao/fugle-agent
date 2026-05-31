# 📅 ★最新版★ 上傳於 2026-05-31 23:22  (原最後更新 2026-05-29)(全新)
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
    a = str(advice or "")
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
    a = str(advice or "")
    if a.startswith("不要") or a.startswith("先別"):
        return "🟡"
    if "賣" in a or "停損" in a or "減碼" in a:
        return "🔴"
    if "買" in a or a.startswith("抱") or a.startswith("續抱"):
        return "🟢"
    if a.startswith("⏳") or "等" in a or a.startswith("留意"):
        return "🟡"
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
    """把 'YYYY-MM-DD HH:MM:SS'(台北)轉成『X分前/X小時前/X天前』+ 是否過久。"""
    s = str(ts or "").strip()
    if not s:
        return ("尚未更新", True)
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
    if mins < 60:
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
    a = str(advice or "")
    if a.startswith("不要") or a.startswith("先別"):
        return "gray"
    if "賣" in a or "停損" in a or "減碼" in a:
        return "danger"
    if "買" in a or a.startswith("抱") or a.startswith("續抱"):
        return "success"
    return "gray"


# ---------------------------------------------------------------------------
def render(trigger_workflow, job_indicator, mark_job_started, cancel_all=None,
           workflow_running=None, cleanup_watchlist=None) -> None:
    st.markdown("""<style>
    .gyh-card div[data-testid="stExpander"]{border:0.5px solid rgba(127,127,127,.2);border-radius:12px;margin-bottom:8px}
    </style>""", unsafe_allow_html=True)

    # ── 標題 + 持有/追蹤切換 ──
    c1, c2 = st.columns([1, 1])
    with c1:
        st.markdown("### 加油好嗎？")
    with c2:
        mode = st.radio("檢視", ["持有", "追蹤"], horizontal=True,
                        label_visibility="collapsed", key="dash_mode")

    # 只更新「你現在看的」那一邊:持有→positions、追蹤→watchlist
    _scope = "positions" if mode == "持有" else "watchlist"
    _scope_zh = "股票部位" if mode == "持有" else "追蹤清單"
    # ── 按鈕狀態(只鎖「這個 scope」在跑的,另一邊不受影響)──
    # 主要動作:持有=新交易更新(positions_new)、追蹤=新追蹤更新(watchlist_new)
    _new_scope = "positions_new" if mode == "持有" else "watchlist_new"
    if workflow_running:
        full_running = workflow_running("full_update.yml", _scope)
        intra_running = workflow_running("intraday_update.yml", _scope)
        new_running = workflow_running("full_update.yml", _new_scope)
    else:
        full_running = "🔄" in job_indicator("full_update")
        intra_running = "🔄" in job_indicator("intraday_update")
        new_running = False
    any_running = full_running or intra_running or new_running
    _f = " 🔄" if full_running else ""
    _i = " 🔄" if intra_running else ""
    _n = " 🔄" if new_running else ""

    # ── 主要動作按鈕(依你做了什麼)──
    if mode == "持有":
        if st.button(f"🆕 新交易更新{_n}",
                     use_container_width=True, disabled=any_running,
                     help="加完買賣交易就按這個:重算部位/損益/實際損益、整理追蹤清單、"
                          "刷新所有持股股價,並只幫『全新沒分析過的股票』補基本面(省錢)"):
            r = trigger_workflow("full_update.yml", inputs={"scope": "positions_new"})
            if r.get("ok"):
                mark_job_started("full_update", 900)
                st.success("✅ 新交易更新已觸發(背景跑)")
                st.rerun()
            else:
                st.error(f"❌ {r.get('error')}")
    else:  # 追蹤
        if st.button(f"🆕 新追蹤更新{_n}",
                     use_container_width=True, disabled=any_running,
                     help="加完追蹤就按這個:只分析追蹤清單裡『還沒分析過』的新代號,"
                          "不重跑已分析的,省錢省時"):
            r = trigger_workflow("full_update.yml", inputs={"scope": "watchlist_new"})
            if r.get("ok"):
                mark_job_started("full_update", 900)
                st.success("✅ 新追蹤更新已觸發(只分析新代號,背景跑)")
                st.rerun()
            else:
                st.error(f"❌ {r.get('error')}")

    # ── 全更新 / 盤中更新(進階:重跑全部)──
    b1, b2 = st.columns(2)
    with b1:
        if st.button(f"🔄 全更新{_f}",
                     use_container_width=True, disabled=any_running,
                     help=f"把「{_scope_zh}」全部重新深入分析一遍(含基本面,慢、較花錢)"):
            r = trigger_workflow("full_update.yml", inputs={"scope": _scope})
            if r.get("ok"):
                mark_job_started("full_update", 900)
                st.success(f"✅ 全更新已觸發(只跑{_scope_zh},背景跑)")
                st.rerun()
            else:
                st.error(f"❌ {r.get('error')}")
    with b2:
        if st.button(f"⚡ 盤中更新{_i}",
                     use_container_width=True, disabled=any_running,
                     help=f"「{_scope_zh}」只刷股價/動能 + 我該做啥(跳過基本面,快、免費)"):
            r = trigger_workflow("intraday_update.yml", inputs={"scope": _scope})
            if r.get("ok"):
                mark_job_started("intraday_update", 180)
                st.success(f"✅ 盤中更新已觸發(只跑{_scope_zh},背景跑)")
                st.rerun()
            else:
                st.error(f"❌ {r.get('error')}")

    if any_running:
        st.caption("⏳ 更新跑中…跑完前按鈕會鎖住,避免重複觸發(可到「更多」終止)")

    if mode == "持有":
        _render_holdings()
    else:
        _render_watchlist()

    # ── 更多(收起來)──
    with st.expander("⚙️ 更多"):
        if st.button("🔁 重新整理", use_container_width=True):
            st.rerun()
        if cleanup_watchlist and st.button(
                "🧹 整理追蹤清單", use_container_width=True,
                help="移除已持有的、重複的留第一個、把賣光過的補回來(追蹤理由=曾經)"):
            with st.spinner("整理中…"):
                res = cleanup_watchlist()
            if res.get("ok"):
                st.success(
                    f"✅ 已整理:移除持有 {res.get('removedHeld', 0)}、"
                    f"去重 {res.get('removedDup', 0)}、補曾經 {res.get('added', 0)}")
            else:
                st.error(f"❌ {res.get('error')}")
            st.cache_data.clear()
            st.rerun()
        if cancel_all and st.button("🛑 終止跑中的更新", use_container_width=True):
            res = cancel_all()
            if res.get("ok"):
                n = res.get("n_cancelled", 0)
                st.success(f"✅ 已取消 {n} 個" if n else "沒有跑中的")
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
        st.info("股票部位是空的 — 先在「股票交易」加交易,再按全更新。")
        return

    # 總額
    mv = sum(_num(_g(r, "市值")) or 0 for r in rows)
    pnl = sum(_num(_g(r, "損益")) or 0 for r in rows)
    realized = _realized_total()
    m1, m2, m3 = st.columns(3)
    m1.metric("總市值", f"{mv:,.0f}")
    m2.metric("總損益", f"{pnl:+,.0f}")
    m3.metric("已實現", f"{realized:+,.0f}")

    _what_to_do_summary(rows, is_position=True)
    _last_update_caption(rows)

    rows.sort(key=lambda r: _action_rank(_g(r, "我該做啥", "綜合建議")))
    st.markdown('<div class="gyh-card">', unsafe_allow_html=True)
    for r in rows:
        _holding_card(r)
    st.markdown('</div>', unsafe_allow_html=True)


def _render_watchlist() -> None:
    try:
        rows = [r for r in (sheets.load_watchlist() or [])
                if not r.get("_error") and _g(r, "代號", "symbol")]
    except Exception as e:
        st.error(f"讀追蹤清單失敗:{e}")
        return
    if not rows:
        st.info("追蹤清單是空的。")
        return

    _what_to_do_summary(rows, is_position=False)
    _last_update_caption(rows)

    rows.sort(key=lambda r: _action_rank(_g(r, "我該做啥", "綜合建議")))
    st.markdown('<div class="gyh-card">', unsafe_allow_html=True)
    for r in rows:
        _watch_card(r)
    st.markdown('</div>', unsafe_allow_html=True)


def _what_to_do_summary(rows: list[dict], is_position: bool) -> None:
    sell, buy, hit = [], [], []
    for r in rows:
        adv = _g(r, "我該做啥", "綜合建議")
        code = _g(r, "代號", "symbol")
        short = _action_short(adv)
        if "趕快賣" in adv or "賣一批" in adv:
            sell.append(code)
        elif "趕快買" in adv or "趕快再買" in adv:
            buy.append(code)
    parts = []
    if sell:
        parts.append(_pill("該賣", "danger") + " " + "、".join(sell))
    if buy:
        parts.append(_pill("趕快買", "success") + " " + "、".join(buy))
    if not parts:
        parts.append('<span style="color:#5F5E5A">目前沒有要趕快動的</span>')
    html = ('<div style="border:0.5px solid rgba(127,127,127,.3);border-radius:12px;'
            'padding:10px 12px;margin:6px 0 4px"><b>⚠️ 該做什麼</b><br>'
            + '<br>'.join(parts) + '</div>')
    st.markdown(html, unsafe_allow_html=True)


def _holding_card(r: dict) -> None:
    code = _g(r, "代號", "symbol")
    name = _g(r, "名稱", "name")
    adv = _g(r, "我該做啥", "綜合建議")
    pnl_pct = _num(_g(r, "損益%"))
    pct_txt = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "—"
    label = f"{_action_emoji(adv)} {_action_short(adv)}　{code} {name}　{pct_txt}"
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
    tag = f"（{reason}）" if reason else ""
    label = f"{_action_emoji(adv)} {_action_short(adv)}　{code} {name}{tag}"
    with st.expander(label):
        _detail_common(r, adv)


def _detail_common(r: dict, adv: str) -> None:
    """順序:損益 → 動能(+白話原因) → 基本面(+白話原因) → 我該做啥(最後)。"""
    mut = "color:#5F5E5A"
    # 1) 損益(持有才有)
    pnl_pct = _g(r, "損益%")
    if pnl_pct:
        pnl = _g(r, "損益")
        try:
            col = "var(--color-text-success)" if float(pnl_pct) >= 0 else "var(--color-text-danger)"
        except ValueError:
            col = "inherit"
        st.markdown(f'💰 <b>損益</b> <span style="color:{col};font-weight:500">{pnl_pct}%'
                    f'{("  " + pnl) if pnl else ""}</span>', unsafe_allow_html=True)
    # 2) 動能(+ 白話原因)
    short = _g(r, "短線燈號")
    mom = _g(r, "動能原因")
    st.markdown(f'<div style="margin-top:8px">⚡ <b>動能</b> {short}'
                + (f'<br><span style="{mut};font-size:13px">← {mom}</span>' if mom else "")
                + '</div>', unsafe_allow_html=True)
    # 3) 基本面 — 拆成「公司面」+「籌碼面」兩塊,各自一個燈 + 白話原因
    ft, fs = _rel_time(_g(r, "基本面資料時間"), 60 * 24 * 5)

    def _fund_block(title: str, light: str, items: list[tuple[str, str]]) -> None:
        lines = [(k, _g(r, k)) for k in items]
        lines = [(k, v) for k, v in lines if v]
        body = "".join(f'<div style="display:flex;justify-content:space-between;font-size:13px;'
                       f'padding:2px 0"><span style="{mut}">{k}</span><span>{v}</span></div>'
                       for k, v in lines)
        st.markdown(f'<div style="margin-top:10px">🏢 <b>{title}</b> {light or "—"}</div>'
                    + body, unsafe_allow_html=True)

    _fund_block("公司面", _g(r, "公司面燈號"),
                ["估值", "配息", "營收動能"])
    _fund_block("籌碼面", _g(r, "籌碼面燈號"),
                ["法人籌碼", "近期新聞重點"])
    st.caption(f"基本面資料 {ft}{' ⚠️舊' if fs else ''}")
    # 4) 我該做啥(最後)
    reason = _action_reason(adv)
    st.markdown('<div style="margin-top:10px;border-top:0.5px solid rgba(127,127,127,.2);'
                'padding-top:8px">👉 <b>我該做啥</b>　'
                + _pill(_action_short(adv), _pill_kind(adv))
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
