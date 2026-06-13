# 🚀【最新上傳 2026-06-13 21:20】portfolio.py — 極簡持股 + 已實現損益(盤中即時股價 + 大盤/美股橫幅)
"""只讀 Google Sheet 的「股票交易」頁 → 用平均成本法算持股 → Fugle 即時報價 → 損益。
另讀「實際損益」頁顯示已實現。兩個分頁,表格可點欄位排序;盤中每 30 秒自動更新。

部署:Streamlit Cloud 把 app 的 Main file 指到 portfolio.py 即可(金鑰沿用現有 secrets)。
"""
from __future__ import annotations

import csv
import io
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

# secrets → env(讓 FugleClient 讀得到金鑰)
for _k in ("FUGLE_MARKETDATA_API_KEY", "FUGLE_API_KEY", "PORTFOLIO_SHEET_URL"):
    if _k in st.secrets and not os.environ.get(_k):
        os.environ[_k] = str(st.secrets[_k])

import math  # noqa: E402

from fugle_agent.client import FugleClient  # noqa: E402

SHEET_FALLBACK = "12xw3HLOq7e7vAogjwbnUwR77ApBfqQQkwMxDuHEKYAo"
DISCOUNT = 0.88          # 賣出手續費折扣(對齊券商 App)
FEE_RATE = 0.001425

st.set_page_config(page_title="加油好嗎?", layout="wide")


def _num(x) -> float:
    try:
        return float(str(x).replace(",", "").replace("%", "").strip())
    except (ValueError, AttributeError, TypeError):
        return 0.0


def _sheet_id() -> str:
    m = re.search(r"/d/([A-Za-z0-9_-]+)", os.environ.get("PORTFOLIO_SHEET_URL", ""))
    return m.group(1) if m else SHEET_FALLBACK


@st.cache_data(ttl=300)
def fetch_tab(tab: str) -> list[dict]:
    sid = _sheet_id()
    url = (f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq"
           f"?tqx=out:csv&sheet={urllib.parse.quote(tab)}&cb={int(time.time())}")
    raw = urllib.request.urlopen(url, timeout=20).read().decode("utf-8")
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return []
    hdr = [h.strip() for h in rows[0]]
    return [dict(zip(hdr, r)) for r in rows[1:]]


def compute_holdings(trades: list[dict]) -> list[dict]:
    """平均成本法:cf=含買進手續費(投資成本);cp=純成交價(成交均價)。"""
    pos: dict[str, dict] = {}
    order: list[str] = []
    for t in trades:
        code = str(t.get("代號") or "").strip()
        if not code:
            continue
        if code not in pos:
            pos[code] = {"name": str(t.get("名稱") or "").strip(), "s": 0.0, "cf": 0.0, "cp": 0.0}
            order.append(code)
        p = pos[code]
        sh, pr, fee = _num(t.get("股數")), _num(t.get("成交價")), _num(t.get("手續費"))
        if str(t.get("動作") or "").lower() == "buy":
            p["s"] += sh; p["cf"] += sh * pr + fee; p["cp"] += sh * pr
        else:
            if p["s"]:
                rr = sh / p["s"]; p["cf"] -= p["cf"] * rr; p["cp"] -= p["cp"] * rr
            p["s"] -= sh
            if p["s"] < 1e-6:
                p["s"] = p["cf"] = p["cp"] = 0.0
    held = []
    for code in order:
        p = pos[code]
        if p["s"] <= 0:
            continue
        held.append({"代號": code, "名稱": p["name"], "股數": round(p["s"]),
                     "成交均價": p["cp"] / p["s"], "投資成本": p["cf"]})
    return held


@st.cache_data(ttl=30)
def live_prices(codes: tuple[str, ...]) -> dict[str, float | None]:
    c = FugleClient()
    out: dict[str, float | None] = {}
    for code in codes:
        try:
            q = c.quote(code) or {}
            p = q.get("lastPrice") or q.get("closePrice") or q.get("price")
            out[code] = float(p) if p is not None else None
        except Exception:
            out[code] = None
        time.sleep(0.3)
    return out


def market_open() -> bool:
    now = datetime.utcnow() + timedelta(hours=8)
    return now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) <= (13, 30)


def _color(v):
    if isinstance(v, (int, float)):
        return "color:#e23b3b" if v >= 0 else "color:#1f9d6b"   # 紅=賺 綠=賠
    return ""


def _bg(light: str) -> tuple[str, str]:
    if "🔴" in light:
        return "rgba(226,75,74,.10)", "rgba(226,75,74,.40)"
    if "🟢" in light:
        return "rgba(99,153,34,.10)", "rgba(99,153,34,.40)"
    return "rgba(127,127,127,.07)", "rgba(127,127,127,.25)"


@st.cache_data(ttl=60)
def taiwan_banner() -> tuple[str, str]:
    try:
        from fugle_agent import market_context
        m = market_context.get_market_context()
        return m.get("light", ""), m.get("reason", "")
    except Exception:
        return "", ""


@st.cache_data(ttl=120)
def us_banner() -> dict:
    out = {"items": {}, "time": "", "light": "🟡 普通"}
    try:
        from fugle_agent import us_market
        asof = ""
        for zh, sym in (("費半", "^SOX"), ("標普", "^GSPC"), ("那斯達克", "^IXIC")):
            q = us_market.quote(sym)
            cp = q.get("changePercent") if isinstance(q, dict) and not q.get("error") else None
            if cp is not None and not (isinstance(cp, float) and math.isnan(cp)):
                out["items"][zh] = float(cp)
                if not asof and q.get("asOf"):
                    asof = str(q["asOf"])[:10]
        if out["items"]:
            out["time"] = us_market.latest_index_time() or asof
            avg = sum(out["items"].values()) / len(out["items"])
            out["light"] = "🟢 偏強" if avg >= 0.5 else ("🔴 偏弱" if avg <= -0.5 else "🟡 普通")
    except Exception:
        pass
    return out


def render_banners() -> None:
    box = ("<div style='background:{bg};border:.5px solid {bd};border-radius:10px;"
           "padding:8px 12px;margin-bottom:8px;font-size:13px'>{html}</div>")
    tl, treason = taiwan_banner()
    if tl:
        bg, bd = _bg(tl)
        st.markdown(box.format(bg=bg, bd=bd, html=f"🌡️ <b>大盤:{tl}</b>　{treason}"),
                    unsafe_allow_html=True)
    us = us_banner()
    if us["items"]:
        bg, bd = _bg(us["light"])
        parts = []
        for zh, pct in us["items"].items():
            col = "#e23b3b" if pct >= 0 else "#1f9d6b"
            parts.append(f'{zh} <span style="color:{col};font-weight:500">{pct:+.1f}%</span>')
        when = us.get("time") or ""
        dtag = f"(資料 {when}・延遲約15分)" if when else "(延遲約15分)"
        st.markdown(box.format(bg=bg, bd=bd,
                    html=f"🌎 <b>美股:{us['light']}</b>　" + " · ".join(parts) + f"　{dtag}"),
                    unsafe_allow_html=True)


# ---------------------------------------------------------------- 版面
st.title("加油好嗎?")
st.caption(("🟢 盤中・每 30 秒自動更新即時股價" if market_open()
            else "⚪ 非盤中・顯示最後成交價") + f"　|　{datetime.utcnow() + timedelta(hours=8):%Y-%m-%d %H:%M:%S}")
if st.button("🔄 立即重抓"):
    live_prices.clear(); fetch_tab.clear(); taiwan_banner.clear(); us_banner.clear(); st.rerun()


@st.fragment(run_every=(60 if market_open() else None))
def banners_view():
    render_banners()


banners_view()

tab_h, tab_r = st.tabs(["持股總覽", "已實現損益"])


@st.fragment(run_every=(30 if market_open() else None))
def holdings_view():
    held = compute_holdings(fetch_tab("股票交易"))
    prices = live_prices(tuple(h["代號"] for h in held))
    recs = []
    for h in held:
        pr = prices.get(h["代號"])
        mv = h["股數"] * pr if pr else None
        if mv is not None:
            tax = 0.001 if h["代號"].startswith("00") else 0.003
            pnl = round(mv - h["投資成本"] - mv * FEE_RATE * DISCOUNT - mv * tax)
            pct = pnl / h["投資成本"] * 100 if h["投資成本"] else None
        else:
            pnl = pct = None
        recs.append({**h, "現價": pr, "現值": mv, "預估損益": pnl, "報酬率": pct})
    df = pd.DataFrame(recs).sort_values("現值", ascending=False, na_position="last")

    mv_t = df["現值"].dropna().sum()
    cost_t = df.loc[df["現值"].notna(), "投資成本"].sum()
    pnl_t = df["預估損益"].dropna().sum()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("總現值", f"{mv_t:,.0f}")
    c2.metric("投資成本", f"{cost_t:,.0f}")
    c3.metric("預估損益", f"{pnl_t:+,.0f}")
    c4.metric("報酬率", f"{(pnl_t / cost_t * 100 if cost_t else 0):+.2f}%")

    df = df[["代號", "名稱", "股數", "成交均價", "現價", "現值", "投資成本", "預估損益", "報酬率"]]
    sty = (df.style
           .format({"股數": "{:,.0f}", "成交均價": "{:.2f}", "現價": "{:.2f}",
                    "現值": "{:,.0f}", "投資成本": "{:,.0f}",
                    "預估損益": "{:+,.0f}", "報酬率": "{:+.1f}%"}, na_rep="—")
           .map(_color, subset=["預估損益", "報酬率"]))
    st.dataframe(sty, hide_index=True, use_container_width=True, height=620)


def realized_view():
    rows = fetch_tab("實際損益")
    recs = []
    for r in rows:
        if not str(r.get("代號") or "").strip():
            continue
        recs.append({"賣出日": str(r.get("賣出日期") or "").strip(),
                     "代號": str(r.get("代號") or "").strip(),
                     "名稱": str(r.get("名稱") or "").strip(),
                     "股數": _num(r.get("賣出股數")), "賣價": _num(r.get("賣價")),
                     "實際損益": _num(r.get("實際損益")), "損益%": _num(r.get("損益%")),
                     "持有天": _num(r.get("持有天數"))})
    df = pd.DataFrame(recs)
    tot = df["實際損益"].sum(); n = len(df)
    win = int((df["實際損益"] > 0).sum()); lose = int((df["實際損益"] < 0).sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("已實現總損益", f"{tot:+,.0f}")
    c2.metric("交易筆數", f"{n}", f"賺 {win} · 賠 {lose}")
    c3.metric("勝率", f"{(win / n * 100 if n else 0):.0f}%")
    c4.metric("平均每筆", f"{(tot / n if n else 0):+,.0f}")
    sty = (df.style
           .format({"股數": "{:,.0f}", "賣價": "{:.2f}", "實際損益": "{:+,.0f}",
                    "損益%": "{:+.1f}%", "持有天": "{:.0f}"}, na_rep="—")
           .map(_color, subset=["實際損益", "損益%"]))
    st.dataframe(sty, hide_index=True, use_container_width=True, height=620)


with tab_h:
    holdings_view()
with tab_r:
    realized_view()
