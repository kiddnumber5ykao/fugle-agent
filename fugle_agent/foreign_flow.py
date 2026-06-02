# 📅 ★最新版★ 上傳於 2026-06-02 (新增)外資每日提醒(證交所/櫃買官方 EOD)
"""外資每日買賣超 — 證交所(上市 T86)+ 櫃買(上櫃)官方收盤後開放資料。

免費、每個交易日**收盤後**才有一筆(官方一天只出一次,盤中不更新)。
拿來:
  1) 把每檔的「籌碼面燈號 + 法人籌碼」改成官方真數字(取代 AI 用新聞猜的)。
  2) 外資從「一直買」翻成「轉賣」(或反過來)時,記進「🔔 今天要注意的」。

用法:from fugle_agent.foreign_flow import update_foreign; update_foreign("all")
"""
from __future__ import annotations

import datetime
import json
import urllib.request

from . import sheets, sheets_writer

_UA = {"User-Agent": "Mozilla/5.0"}


def _now_tw() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _to_int(s) -> int | None:
    try:
        return int(str(s).replace(",", "").replace(" ", "").strip())
    except Exception:
        return None


def _clean_sym(s) -> str:
    return str(s or "").replace("'", "").strip()


# ───────────────────────── 上市:證交所 T86 ─────────────────────────
def _fetch_twse(d: datetime.date) -> dict[str, int]:
    """回 {代號: 外資買賣超『股數』}。假日 / 沒資料 → {}。"""
    ymd = d.strftime("%Y%m%d")
    url = (f"https://www.twse.com.tw/rwd/zh/fund/T86"
           f"?date={ymd}&selectType=ALL&response=json")
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        j = json.load(r)
    if j.get("stat") != "OK":
        return {}
    fields = j.get("fields", [])
    # 找「外陸資買賣超股數(不含外資自營商)」那一欄 — 用欄名比對,不寫死位置
    idx = None
    for i, f in enumerate(fields):
        fn = str(f).replace(" ", "")
        if ("買賣超" in fn and ("外陸資" in fn or "外資及陸資" in fn)
                and "自營商買賣超" not in fn):
            idx = i
            break
    if idx is None:
        return {}
    out: dict[str, int] = {}
    for row in j.get("data", []):
        if not row or len(row) <= idx:
            continue
        sym = _clean_sym(row[0])
        net = _to_int(row[idx])
        if sym and net is not None:
            out[sym] = net
    return out


# ───────────────────────── 上櫃:櫃買中心 ─────────────────────────
def _fetch_tpex(d: datetime.date) -> dict[str, int]:
    """上櫃三大法人(外資)買賣超。

    ⚠️ v1 先停用 — 櫃買中心的欄位排版還沒實測,寧可不寫也別寫錯數字。
    停用時上櫃股票會保留原本的籌碼面燈(不會被亂改)。
    之後實測過 TPEx 格式、確認外資買賣超在哪一欄,再把下面打開即可。"""
    return {}


# ───────────────────────── 抓最近幾個交易日 ─────────────────────────
def fetch_recent_foreign(symbols: set[str], days_needed: int = 3,
                         lookback: int = 12) -> dict[str, list[tuple[str, int]]]:
    """回 {代號: [(日期, 張數), ...]},最近的交易日在最前面,最多 days_needed 天。
    股數 → 張(÷1000)。假日自動跳過(抓回空)。"""
    want = {s for s in symbols if s}
    series: dict[str, list[tuple[str, int]]] = {s: [] for s in want}
    got = 0
    today = _now_tw().date()
    for i in range(lookback):
        d = today - datetime.timedelta(days=i)
        if d.weekday() >= 5:        # 六日直接跳
            continue
        try:
            twse = _fetch_twse(d)
        except Exception:
            twse = {}
        tpex = {}
        try:
            tpex = _fetch_tpex(d)
        except Exception:
            tpex = {}
        if not twse and not tpex:   # 假日 / 還沒出
            continue
        ds = d.strftime("%Y-%m-%d")
        for s in want:
            net = twse.get(s)
            if net is None:
                net = tpex.get(s)
            if net is not None:
                series[s].append((ds, round(net / 1000)))   # 股 → 張
        got += 1
        if got >= days_needed:
            break
    return series


def _sgn(x: int) -> str:
    return "買" if x > 0 else ("賣" if x < 0 else "平")


def foreign_light(hist: list[tuple[str, int]]) -> tuple[str, str, str, str]:
    """hist 最新在前 [(日期, 張), ...]。
    回 (籌碼面燈號, 法人籌碼白話, 今天方向, 資料日)。"""
    if not hist:
        return ("⚪ 資料不足", "查不到外資資料", "無", "")
    ddate, net0 = hist[0]
    today_dir = _sgn(net0)
    # 連續同方向天數
    streak = 0
    for _, n in hist:
        if today_dir != "平" and _sgn(n) == today_dir:
            streak += 1
        else:
            break
    amt = f"{abs(net0):,}張"
    if today_dir == "買":
        light = "🟢 在買"
        txt = (f"外資連{streak}天買超(今買 {amt})" if streak >= 2
               else f"外資今天買超 {amt}")
    elif today_dir == "賣":
        light = "🔴 在賣"
        txt = (f"外資連{streak}天賣超(今賣 {amt})" if streak >= 2
               else f"外資今天賣超 {amt}")
    else:
        light = "🟡 沒動作"
        txt = "外資今天沒明顯買賣"
    return (light, txt, today_dir, ddate)


def detect_flip(hist: list[tuple[str, int]]) -> tuple[str, str] | None:
    """今天方向 vs 前一交易日方向。翻轉回 (方向, 白話);否則 None。"""
    if len(hist) < 2:
        return None
    d_now, d_prev = _sgn(hist[0][1]), _sgn(hist[1][1])
    if d_now == "平" or d_prev == "平" or d_now == d_prev:
        return None
    if d_now == "買":
        return ("買", "外資從賣轉買了,有人開始進場")
    return ("賣", "外資從買轉賣了,大戶開始出貨,手上有的要當心")


# ───────────────────────── 主流程 ─────────────────────────
def update_foreign(scope: str = "all") -> dict:
    """讀持股 + 追蹤的代號 → 抓官方外資 → 寫籌碼面燈號/法人籌碼 → 翻轉記進🔔。"""
    do_pos = scope in ("all", "positions", "positions_new")
    do_wl = scope in ("all", "watchlist", "watchlist_new")

    pos_rows = sheets.load_positions() if do_pos else []
    wl_rows = sheets.load_watchlist() if do_wl else []

    name_of: dict[str, str] = {}
    pos_syms: set[str] = set()
    wl_syms: set[str] = set()
    for r in (pos_rows or []):
        if r.get("_error"):
            continue
        s = _clean_sym(r.get("代號") or r.get("symbol"))
        if s:
            pos_syms.add(s)
            name_of[s] = str(r.get("名稱") or r.get("name") or "").strip()
    for r in (wl_rows or []):
        if r.get("_error"):
            continue
        s = _clean_sym(r.get("代號") or r.get("symbol"))
        if s:
            wl_syms.add(s)
            name_of.setdefault(s, str(r.get("名稱") or r.get("name") or "").strip())

    all_syms = pos_syms | wl_syms
    if not all_syms:
        print("   ℹ️ 沒有代號,外資略過", flush=True)
        return {"ok": True, "updated": 0, "flips": 0}

    series = fetch_recent_foreign(all_syms)
    now = _now_tw().strftime("%Y-%m-%d %H:%M:%S")
    changes: list[dict] = []
    updated = 0

    for s in sorted(all_syms):
        hist = series.get(s) or []
        light, txt, _dir, ddate = foreign_light(hist)
        if light.startswith("⚪"):
            continue   # 查不到就別覆寫舊的
        payload = {"symbol": s, "代號": s,
                   "籌碼面燈號": light, "法人籌碼": f"{txt}（{ddate}）"}
        nm = name_of.get(s) or ""
        if nm:
            payload["name"] = nm
            payload["名稱"] = nm
        try:
            if s in pos_syms:
                sheets_writer.upsert_position(**payload)
            if s in wl_syms:
                sheets_writer.upsert_watchlist_item(**payload)
            updated += 1
        except Exception as e:
            print(f"   ⚠️ 寫入 {s} 外資失敗: {e}", flush=True)

        flip = detect_flip(hist)
        if flip:
            changes.append({
                "time": now,
                "scope": "持有" if s in pos_syms else "追蹤",
                "symbol": s, "name": nm,
                "dir": flip[0], "msg": flip[1],
            })

    if changes:
        try:
            sheets_writer.log_changes(changes)
        except Exception as e:
            print(f"   ⚠️ 寫入外資翻轉提醒失敗: {e}", flush=True)

    print(f"   🐳 外資更新 {updated} 檔 / 翻轉 {len(changes)} 檔", flush=True)
    return {"ok": True, "updated": updated, "flips": len(changes)}
