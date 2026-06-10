#!/usr/bin/env python3
# 🚀【最新待上傳 2026-06-09 · DEPLOY-0609】watch_alerts.py — 盯盤即時提醒(第一版)
"""讀「照哥計劃表」(代號/進場價/目標價/啟用),盤中每 POLL_SEC 秒比現價:
靠近進場價(±NEAR_PCT%、且還沒漲過目標)就推一則 Telegram,告訴你「離進場價%、離目標剩餘空間%」,
你自己決定要不要進。不下命令、不洗版(進靠近區推一次,離開再進來才會再推)。

環境變數(由 GitHub Actions 帶):
  FUGLE_MARKETDATA_API_KEY / PORTFOLIO_SHEET_URL / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
  PLAN_TAB(預設 照哥計劃表)/ NEAR_PCT(預設 2)/ POLL_SEC(預設 60)/ MAX_MIN(預設 270)
"""
from __future__ import annotations

import datetime
import os
import time
import urllib.parse
import urllib.request

from fugle_agent import sheets
from fugle_agent.client import FugleClient

PLAN_TAB = os.getenv("PLAN_TAB", "照哥計劃表")
NEAR_PCT = float(os.getenv("NEAR_PCT", "2"))
POLL_SEC = int(os.getenv("POLL_SEC", "60"))
MAX_MIN = int(os.getenv("MAX_MIN", "270"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def _tw_now() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _f(v):
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError, AttributeError):
        return None


def tg_send(text: str) -> None:
    print("TG>", text.replace("\n", " | "), flush=True)
    if not TG_TOKEN or not TG_CHAT:
        print("⚠️ 沒有 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID,跳過推播", flush=True)
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15).read()
    except Exception as e:
        print(f"⚠️ Telegram 推播失敗: {type(e).__name__}: {e}", flush=True)


def load_plan() -> list[dict]:
    rows = sheets.fetch_tab(PLAN_TAB) or []
    plan = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = str(r.get("代號") or r.get("symbol") or "").lstrip("'").strip()
        if not code:
            continue
        if str(r.get("啟用") or "").strip() not in ("1", "✓", "v", "V", "是", "true", "True"):
            continue
        p_in = _f(r.get("進場價"))
        if p_in is None:
            continue
        plan.append({"code": code, "in": p_in, "tgt": _f(r.get("目標價"))})
    return plan


def get_price(c: FugleClient, code: str):
    try:
        q = c.quote(code) or {}
        return (_f(q.get("lastPrice")) or _f(q.get("closePrice")) or _f(q.get("price")))
    except Exception:
        return None


def _line(code, price, p_in, p_tgt) -> str:
    d_in = (price - p_in) / p_in * 100
    s = f"{code} 現價 {price:g}　進場 {p_in:g}（{d_in:+.1f}%）"
    if p_tgt:
        s += f"　目標 {p_tgt:g}（剩 +{(p_tgt - price) / price * 100:.1f}%）"
    return s


def main() -> None:
    plan = load_plan()
    if not plan:
        tg_send(f"📡 盯盤啟動,但「{PLAN_TAB}」沒有啟用(=1)的股票,請檢查分頁。")
        return
    c = FugleClient()

    # 啟動快照:每檔現價 + 距離(確認 Telegram + 讀到計劃 + 抓得到價)
    snap = [f"📡 盯盤啟動,盯 {len(plan)} 檔(每 {POLL_SEC}s,靠近 {NEAR_PCT}% 才提醒):"]
    for p in plan:
        price = get_price(c, p["code"])
        snap.append("・" + (_line(p["code"], price, p["in"], p["tgt"])
                            if price is not None else f"{p['code']} 現價抓不到"))
    tg_send("\n".join(snap))

    alerted: dict[str, bool] = {}
    t0 = time.time()
    while (time.time() - t0) < MAX_MIN * 60:
        now = _tw_now()
        if now.weekday() >= 5 or (now.hour, now.minute) >= (13, 35):
            print("收盤/非交易時段,結束盯盤", flush=True)
            break
        for p in plan:
            price = get_price(c, p["code"])
            if price is None:
                continue
            d_in = (price - p["in"]) / p["in"] * 100        # 離進場價%(+貴 −便宜)
            near = abs(d_in) <= NEAR_PCT                     # 離進場價 ±NEAR% 以內才算「靠近」
            below_tgt = (p["tgt"] is None) or (price < p["tgt"])
            in_zone = near and below_tgt
            if in_zone and not alerted.get(p["code"]):
                msg = [f"🔔 {p['code']} 到價附近了", _line(p["code"], price, p["in"], p["tgt"]),
                       ("已到/更便宜,要不要低接你決定。" if d_in <= 0 else "快到進場價,要不要進你決定。")]
                tg_send("\n".join(msg))
                alerted[p["code"]] = True
            elif not near:
                alerted[p["code"]] = False                  # 離開靠近區 → 重置,下次再靠近會再提醒
        time.sleep(POLL_SEC)
    print("watcher 結束", flush=True)


if __name__ == "__main__":
    main()
