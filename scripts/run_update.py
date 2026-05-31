#!/usr/bin/env python3
# 📅 ★最新版★ 上傳於 2026-05-31 20:54  (原最後更新 2026-05-29)(全新:全更新/盤中更新一條龍)
"""一條龍更新 — 對應手機儀表板的「全更新 / 盤中更新」按鈕。

用法:
    python scripts/run_update.py full       # 全更新:重算交易→技術→基本面→重算我該做啥
    python scripts/run_update.py intraday    # 盤中更新:重算交易→技術→重算我該做啥(跳過基本面)

順序保證正確(同一個 process 依序跑),所以「我該做啥」永遠最後才定稿。

需要的環境變數(GitHub Secrets):
    ANTHROPIC_API_KEY, FUGLE_MARKETDATA_API_KEY, SHEETS_WRITER_URL, PORTFOLIO_SHEET_URL
"""
from __future__ import annotations

import asyncio
import datetime
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _check_env() -> None:
    required = ["ANTHROPIC_API_KEY", "FUGLE_MARKETDATA_API_KEY",
                "SHEETS_WRITER_URL", "PORTFOLIO_SHEET_URL"]
    missing = [k for k in required if not (os.getenv(k) or "").strip()]
    if missing:
        print(f"❌ 缺環境變數: {missing}")
        sys.exit(1)


def main() -> None:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "intraday").lower().strip()
    if mode not in ("full", "intraday"):
        print(f"❌ 不認識的模式: {mode!r} (要 'full' 或 'intraday')")
        sys.exit(2)

    _check_env()
    os.environ.setdefault("FUGLE_MOCK", "0")

    from fugle_agent.tools import (resync_and_fill_names, organize_all_technical,
                                   organize_all_deep, _recompute_advice)

    t0 = datetime.datetime.now()
    print(f"▶️  開始 {mode} 更新 @ {t0.isoformat(timespec='seconds')}")

    # Step 1: 重算交易 + 補名稱
    print("▶️  Step 1: 重算交易 + 補名稱 …")
    r = resync_and_fill_names()
    if not r.get("ok"):
        print(f"❌ 重算交易失敗: {r.get('error')}")
        sys.exit(3)
    print(f"   ✅ 補名稱:追蹤 {r.get('watchlist')} / 部位 {r.get('positions')} / "
          f"實際損益 {r.get('realized')}")

    # Step 2: 技術面(部位 + 追蹤清單)
    print("▶️  Step 2: 技術面 …")
    asyncio.run(organize_all_technical.handler({"scope": "all"}))

    # Step 3:(只有全更新)基本面
    if mode == "full":
        print("▶️  Step 3: 基本面 …")
        asyncio.run(organize_all_deep.handler({"scope": "all"}))
    else:
        print("⏭  盤中更新跳過基本面(沿用上次全更新的)")

    # Step 4: 最後重算「我該做啥」— 讀齊 4 燈才定稿
    print("▶️  Step 4: 重算我該做啥 …")
    adv = _recompute_advice("all")
    print(f"   🧭 部位 {adv.get('positions')} / 追蹤 {adv.get('watchlist')}")

    dt = (datetime.datetime.now() - t0).total_seconds()
    print(f"✅ {mode} 更新完成 @ +{dt:.1f}s")


if __name__ == "__main__":
    main()
