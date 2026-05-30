# 📅 最後更新:2026-05-29(全新)
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
    s = str(light or "")
    if "⚪" in s or "資料不足" in s or not s.strip():
        return "⚪"
    if "🟢" in s:
        return "🟢"
    if "🔴" in s:
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
    """排序鍵:趕快(0) → 再等等(1) → 不要買(2) → 等分析(3) → 資料不足(4)。
    同層:賣優先於買。"""
    a = str(advice or "")
    if a.startswith("趕快"):
        tier = 0
    elif a.startswith("再等等"):
        tier = 1
    elif a.startswith("不要"):
        tier = 2
    elif "等基本面" in a or "等技術" in a or a.startswith("⏳"):
        tier = 3
    else:
        tier = 4
    sub = 0 if "賣" in a else 1
    return (tier, sub)


def _action_emoji(advice: str) -> str:
    a = str(advice or "")
    if "賣" in a:
        return "🔴"
    if "買" in a:
        return "🟢"
    if a.startswith("⏳") or "等" in a:
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
    if "賣" in a or a.startswith("不要"):
        return "danger" if "賣" in a else "gray"
    if "買" in a and "不要" not in a:
        return "success"
    return "gray"


# ---------------------------------------------------------------------------
def render(trigger_workflow, job_indicator, mark_job_started) -> None:
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

    # ── 全更新 / 盤中更新 ──(跑中時禁用,避免重複按)
    full_ind = job_indicator("full_update")
    intra_ind = job_indicator("intraday_update")
    full_running = "🔄" in full_ind
    intra_running = "🔄" in intra_ind
    # 全更新跑的時候,盤中更新也一起鎖(它們會互相覆寫)
    any_running = full_running or intra_running
    b1, b2 = st.columns(2)
    with b1:
        if st.button(f"🔄 全更新{full_ind}",
                     use_container_width=True, disabled=any_running,
                     help="重算交易 → 技術面 → 基本面 → 重算我該做啥(全部,慢,一天一次或想完整檢討時按)"):
            r = trigger_workflow("full_update.yml")
            if r.get("ok"):
                mark_job_started("full_update", 900)
                st.success("✅ 全更新已觸發(背景跑,約幾分鐘)")
                st.rerun()
            else:
                st.error(f"❌ {r.get('error')}")
    with b2:
        if st.button(f"⚡ 盤中更新{intra_ind}",
                     use_container_width=True, disabled=any_running,
                     help="重算交易 → 技術面 → 重算我該做啥(跳過基本面,快)"):
            r = trigger_workflow("intraday_update.yml")
            if r.get("ok"):
                mark_job_started("intraday_update", 180)
                st.success("✅ 盤中更新已觸發(背景跑,約 1 分鐘)")
                st.rerun()
            else:
                st.error(f"❌ {r.get('error')}")
    if any_running:
        st.caption("⏳ 更新跑中…跑完前按鈕會鎖住,避免重複觸發(可到 sidebar 終止)")

    if mode == "持有":
        _render_holdings()
    else:
        _render_watchlist()


def _last_update_caption(rows: list[dict]) -> None:
    tech = max((_g(r, "技術整理時間") for r in rows), default="")
    fund = max((_g(r, "基本面整理時間") for r in rows), default="")
    tt, ts = _rel_time(tech, 30)
    ft, fs = _rel_time(fund, 60 * 24 * 3)
    warn = ""
    if ts:
        warn += "　⚠️ 技術面有點久了"
    st.caption(f"更新於 — 技術面 {tt} · 基本面 {ft}{warn}")


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
        if "趕快賣" in adv:
            sell.append(code)
        elif "趕快買" in adv or "趕快再買" in adv:
            buy.append(code)
    parts = []
    if sell:
        parts.append(_pill("趕快賣", "danger") + " " + "、".join(sell))
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
    # 該做啥 + 原因
    reason = _action_reason(adv)
    st.markdown(_pill(_action_short(adv), _pill_kind(adv))
                + (f'　<span style="color:#5F5E5A">{reason}</span>' if reason else ""),
                unsafe_allow_html=True)
    # 燈號
    short, sup = _g(r, "短線燈號"), _g(r, "超短線燈號")
    chips, comp = _g(r, "籌碼面燈號"), _g(r, "公司面燈號")
    st.markdown(
        f'<div style="margin-top:8px;font-size:13px">'
        f'<span style="color:#5F5E5A">技術面</span>　{_dots([short, sup])}'
        f'<span style="color:#5F5E5A">短線 / 超短線</span><br>'
        f'<span style="color:#5F5E5A">基本面</span>　{_dots([chips, comp])}'
        f'<span style="color:#5F5E5A">籌碼 / 公司</span></div>',
        unsafe_allow_html=True)
    # 技術描述
    tech_lines = [(k, _g(r, k)) for k in
                  ("最新表現", "最近3天", "這週氛圍", "近10天走勢", "量能變化", "離20天高低")]
    tech_lines = [(k, v) for k, v in tech_lines if v]
    tt, ts = _rel_time(_g(r, "技術整理時間"), 30)
    if tech_lines:
        body = "".join(f'<div style="display:flex;justify-content:space-between;font-size:13px;'
                       f'padding:2px 0"><span style="color:#5F5E5A">{k}</span><span>{v}</span></div>'
                       for k, v in tech_lines)
        st.markdown(f'<div style="margin-top:8px"><b style="font-size:13px">技術面</b>'
                    f' <span style="color:{"#A32D2D" if ts else "#5F5E5A"};font-size:12px">· {tt}'
                    f'{" ⚠️久" if ts else ""}</span>{body}</div>', unsafe_allow_html=True)
    # 基本面描述
    fund_lines = [(k, _g(r, k)) for k in
                  ("估值", "配息", "營收動能", "法人籌碼", "近期新聞重點")]
    fund_lines = [(k, v) for k, v in fund_lines if v]
    ft, fs = _rel_time(_g(r, "基本面整理時間"), 60 * 24 * 3)
    if fund_lines:
        body = "".join(f'<div style="display:flex;justify-content:space-between;font-size:13px;'
                       f'padding:2px 0"><span style="color:#5F5E5A">{k}</span><span>{v}</span></div>'
                       for k, v in fund_lines)
        st.markdown(f'<div style="margin-top:8px"><b style="font-size:13px">基本面</b>'
                    f' <span style="color:{"#A32D2D" if fs else "#5F5E5A"};font-size:12px">· {ft}'
                    f'{" ⚠️久" if fs else ""}</span>{body}</div>', unsafe_allow_html=True)


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
