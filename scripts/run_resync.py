#!/usr/bin/env python3
"""GitHub Actions 排程入口 — 重算交易 + 補名稱。

對應 Streamlit 上的「📋 重算交易+補名稱」按鈕。
做兩件事:
  1. 打 Apps Script manual_sync — 把股票交易重算成股票部位、實際損益、目標賣價公式
  2. 掃股票部位 + 追蹤清單,把沒有「名稱」的補上(用 Fugle / yfinance 查)

需要的環境變數(在 GitHub Secrets 設定):
    ANTHROPIC_API_KEY (其實這支沒用到,但 sheets_writer 共用 env 載入時要在)
    FUGLE_MARKETDATA_API_KEY
    SHEETS_WRITER_URL
    PORTFOLIO_SHEET_URL
"""
from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

# 把 repo 根目錄加進 sys.path,讓我們能 import fugle_agent
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _check_env() -> None:
    required = [
        "FUGLE_MARKETDATA_API_KEY",
        "SHEETS_WRITER_URL",
        "PORTFOLIO_SHEET_URL",
    ]
    missing = [k for k in required if not (os.getenv(k) or "").strip()]
    if missing:
        print(f"❌ 缺環境變數: {missing}")
        sys.exit(1)


def main() -> None:
    _check_env()
    os.environ.setdefault("FUGLE_MOCK", "0")

    from fugle_agent import sheets as _sh
    from fugle_agent import sheets_writer as _sw
    from fugle_agent.tools import _lookup_stock_name

    t0 = datetime.datetime.now()
    print(f"▶️  開始跑 重算+補名稱 @ {t0.isoformat(timespec='seconds')}")
    name_cache: dict[str, str] = {}
    n_filled_pos = 0
    n_filled_wl = 0

    # Step 1: 先補「追蹤清單」名稱 — 放在 manual_sync 之前,
    # 這樣等下 manual_sync 重建「實際損益」時,連已賣光的股票也查得到名稱
    #(Apps Script 的名稱對照表會讀追蹤清單)。
    print("▶️  Step 1/4: 先補追蹤清單名稱 …")
    try:
        for w in (_sh.load_watchlist() or []):
            if w.get("_error"):
                continue
            sym = str(w.get("symbol") or w.get("代號") or "").strip()
            cur_name = str(w.get("name") or w.get("名稱") or "").strip()
            if not sym or cur_name:
                continue
            new_name = name_cache.get(sym) or _lookup_stock_name(sym)
            if not new_name:
                continue
            name_cache[sym] = new_name
            wb = _sw.upsert_watchlist_item(
                symbol=sym, 代號=sym,
                name=new_name, 名稱=new_name)
            if wb.get("ok"):
                n_filled_wl += 1
    except Exception as e:
        print(f"⚠️ 補追蹤清單名稱失敗: {type(e).__name__}: {e}")
    print(f"   追蹤清單補了 {n_filled_wl} 筆")

    # Step 2: 打 Apps Script manual_sync(從股票交易重建股票部位 + 實際損益)
    print("▶️  Step 2/4: manual_sync …")
    try:
        sync_res = _sw.manual_sync(timeout=120)
    except Exception as e:
        print(f"❌ manual_sync exception: {type(e).__name__}: {e}")
        sys.exit(3)
    if not sync_res.get("ok"):
        print(f"❌ manual_sync failed: {sync_res.get('error')}")
        sys.exit(3)
    dt1 = (datetime.datetime.now() - t0).total_seconds()
    print(f"   ✅ manual_sync 完成 @ +{dt1:.1f}s")

    # Step 3: 補「股票部位」名稱(重建後若還有空白)
    print("▶️  Step 3/4: 補股票部位名稱 …")
    try:
        for p in (_sh.load_positions() or []):
            if p.get("_error"):
                continue
            sym = str(p.get("symbol") or "").strip()
            cur_name = str(p.get("name") or "").strip()
            if not sym or cur_name:
                continue
            new_name = name_cache.get(sym) or _lookup_stock_name(sym)
            if not new_name:
                continue
            name_cache[sym] = new_name
            wb = _sw.upsert_position(
                symbol=sym, 代號=sym,
                name=new_name, 名稱=new_name)
            if wb.get("ok"):
                n_filled_pos += 1
    except Exception as e:
        print(f"⚠️ 補股票部位名稱失敗: {type(e).__name__}: {e}")

    # Step 4: 直接掃「實際損益」名稱空白的 row,現查現填(不依賴部位 / 追蹤清單)
    print("▶️  Step 4/4: 補實際損益名稱(獨立直填)…")
    n_filled_realized = 0
    try:
        realized_rows = _sh.fetch_tab("實際損益") or []
        need: dict[str, str] = {}
        for r in realized_rows:
            if r.get("_error"):
                continue
            sym = str(r.get("symbol") or r.get("代號") or "").strip()
            name = str(r.get("name") or r.get("名稱") or "").strip()
            if sym and not name and sym not in need:
                looked = name_cache.get(sym) or _lookup_stock_name(sym)
                if looked:
                    need[sym] = looked
                    name_cache[sym] = looked
        if need:
            wb = _sw.backfill_realized_names(need)
            n_filled_realized = wb.get("filled", 0) if wb.get("ok") else 0
            if not wb.get("ok"):
                print(f"   ⚠️ 寫入失敗: {wb.get('error')}")
        print(f"   實際損益補了 {n_filled_realized} 筆(查到名字 {len(need)} 檔)")
    except Exception as e:
        print(f"⚠️ 補實際損益名稱失敗: {type(e).__name__}: {e}")

    dt = (datetime.datetime.now() - t0).total_seconds()
    print(f"✅ 全部完成 @ +{dt:.1f}s")
    print(f"   補了 {n_filled_pos} 筆部位名稱、{n_filled_wl} 筆追蹤清單名稱、"
          f"{n_filled_realized} 筆實際損益名稱")


if __name__ == "__main__":
    main()
