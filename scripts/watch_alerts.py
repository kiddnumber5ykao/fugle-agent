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
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# 從 scripts/ 跑時,把 repo 根目錄加進路徑,才找得到 fugle_agent
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fugle_agent import pro_signals, sheets  # noqa: E402
from fugle_agent.client import FugleClient  # noqa: E402

PLAN_TAB = os.getenv("PLAN_TAB", "照哥計劃表")
NEAR_PCT = float(os.getenv("NEAR_PCT", "2"))
POLL_SEC = int(os.getenv("POLL_SEC", "60"))
MAX_MIN = int(os.getenv("MAX_MIN", "270"))
STOCK_GAP = float(os.getenv("STOCK_GAP", "0.6"))   # 每檔之間間隔(秒),避免 Fugle 被一次打太多限流
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
        # 進場價可留空 = 「不設價,技術面變好買點就通知」模式
        plan.append({"code": code, "name": str(r.get("名稱") or r.get("name") or "").strip(),
                     "in": _f(r.get("進場價")), "tgt": _f(r.get("目標價"))})
    return plan


def fill_names(plan: list[dict]) -> None:
    """名稱欄空的,自動查官方中文名,回填進「照哥計劃表」(代號為 key)。"""
    to_write = []
    for p in plan:
        if p.get("name"):
            continue
        try:
            from fugle_agent import free_fetch
            nm = (free_fetch.get_name(p["code"]) or "").strip()
        except Exception:
            nm = ""
        if nm:
            p["name"] = nm
            to_write.append({"代號": p["code"], "名稱": nm})
    if to_write:
        try:
            from fugle_agent import sheets_writer
            sheets_writer.bulk_upsert(PLAN_TAB, to_write, key="代號")
            print(f"✍️ 回填名稱 {len(to_write)} 檔到「{PLAN_TAB}」", flush=True)
        except Exception as e:
            print(f"⚠️ 回填名稱失敗: {type(e).__name__}: {e}", flush=True)


def get_price(c: FugleClient, code: str):
    last_err = ""
    for _ in range(3):                 # 限流/暫時失敗 → 重試最多 3 次
        try:
            q = c.quote(code) or {}
            p = (_f(q.get("lastPrice")) or _f(q.get("closePrice")) or _f(q.get("price")))
            if p is not None:
                return p
            last_err = f"回應無價格(keys={list(q)[:8]})"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(0.5)
    print(f"⚠️ {code} 抓不到價:{last_err}", flush=True)   # Actions log 會看到真正原因
    return None


def _tag(p) -> str:
    return f"{p['code']} {p.get('name', '')}".strip()


def _line(tag, price, p_in, p_tgt) -> str:
    s = f"{tag} 現價 {price:g}"
    if p_in:
        s += f"　進場 {p_in:g}（{(price - p_in) / p_in * 100:+.1f}%）"
    if p_tgt:
        s += f"　目標 {p_tgt:g}（剩 +{(p_tgt - price) / price * 100:.1f}%）"
    return s


def main() -> None:
    plan = load_plan()
    if not plan:
        tg_send(f"📡 盯盤啟動,但「{PLAN_TAB}」沒有啟用(=1)的股票,請檢查分頁。")
        return
    fill_names(plan)            # 名稱空的 → 自動查官方中文名、回填進分頁
    c = FugleClient()

    # 啟動快照:每檔現價 + 距離(確認 Telegram + 讀到計劃 + 抓得到價)
    snap = [f"📡 盯盤啟動,盯 {len(plan)} 檔(每 {POLL_SEC}s,靠近 {NEAR_PCT}% 才提醒):"]
    for p in plan:
        price = get_price(c, p["code"])
        time.sleep(STOCK_GAP)
        if price is None:
            snap.append(f"・{_tag(p)} 現價抓不到")
            continue
        line = "・" + _line(_tag(p), price, p["in"], p["tgt"])
        try:
            candles = c.intraday_candles(p["code"]) or {}
            ticks = c.intraday_ticks(p["code"], limit=2000) or {}
            v = pro_signals.entry_verdict(price, candles, ticks)
            line += f"\n　{v['emoji']} {v['headline']}"
        except Exception:
            pass
        snap.append(line)
    tg_send("\n".join(snap))

    alerted: dict[str, bool] = {}
    t0 = time.time()
    while (time.time() - t0) < MAX_MIN * 60:
        now = _tw_now()
        if now.weekday() >= 5 or (now.hour, now.minute) >= (13, 35):
            print("收盤/非交易時段,結束盯盤", flush=True)
            break
        cycle_start = time.time()
        plan = load_plan()          # 每輪重讀計劃表 → 改 Sheet 約 1 分鐘內自動生效,不用重啟
        fill_names(plan)
        for p in plan:
            price = get_price(c, p["code"])
            time.sleep(STOCK_GAP)
            if price is None:
                continue

            # === 模式二:沒設進場價 → 不管價位,技術面變「好買點(🟢)」就通知 ===
            if p["in"] is None:
                try:
                    candles = c.intraday_candles(p["code"]) or {}
                    ticks = c.intraday_ticks(p["code"], limit=2000) or {}
                    v = pro_signals.entry_verdict(price, candles, ticks)
                except Exception as e:
                    print(f"⚠️ 判斷算不出 {p['code']}: {type(e).__name__}", flush=True)
                    continue
                if v["emoji"] == "🟢" and not alerted.get(p["code"]):
                    tg_send("\n".join([f"🟢 {_tag(p)} 看起來是好買點了!",
                                       _line(_tag(p), price, p["in"], p["tgt"]),
                                       f"（{'、'.join(v['reasons'][:4])}）", "你決定。"]))
                    alerted[p["code"]] = True
                elif v["emoji"] != "🟢":
                    alerted[p["code"]] = False               # 不再是好買點 → 重置,下次變好再通知
                continue

            # === 模式一:有設進場價 → 靠近買價就通知(附判斷)===
            d_in = (price - p["in"]) / p["in"] * 100        # 離進場價%(+貴 −便宜)
            near = abs(d_in) <= NEAR_PCT                     # 離進場價 ±NEAR% 以內才算「靠近」
            below_tgt = (p["tgt"] is None) or (price < p["tgt"])
            in_zone = near and below_tgt
            if in_zone and not alerted.get(p["code"]):
                head = "已到/更便宜了" if d_in <= 0 else "快到買價"
                msg = [f"🔔 {_tag(p)} {head}", _line(_tag(p), price, p["in"], p["tgt"])]
                # 算「現在該不該出手」白話判斷(只在要通知時才抓分鐘K+逐筆)
                try:
                    candles = c.intraday_candles(p["code"]) or {}
                    ticks = c.intraday_ticks(p["code"], limit=2000) or {}
                    v = pro_signals.entry_verdict(price, candles, ticks)
                    msg.append(f"{v['emoji']} {v['headline']}（{'、'.join(v['reasons'][:4])}）")
                except Exception as e:
                    print(f"⚠️ 判斷算不出 {p['code']}: {type(e).__name__}", flush=True)
                msg.append("你決定。")
                tg_send("\n".join(msg))
                alerted[p["code"]] = True
            elif not near:
                alerted[p["code"]] = False                  # 離開靠近區 → 重置,下次再靠近會再提醒
        # 整輪固定約 POLL_SEC 一圈(跑得快就補睡、跑滿就不睡)→ 檔數多也不會越拖越久
        rest = POLL_SEC - (time.time() - cycle_start)
        if rest > 0:
            time.sleep(rest)
    print("watcher 結束", flush=True)


if __name__ == "__main__":
    main()
