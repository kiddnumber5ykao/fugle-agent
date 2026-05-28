#!/usr/bin/env python3
"""GitHub Actions 排程入口 — 跑技術分析或深度分析,寫回 Google Sheet。

用法:
    python scripts/run_scheduled.py technical
    python scripts/run_scheduled.py deep

需要的環境變數(在 GitHub Secrets 設定):
    ANTHROPIC_API_KEY
    FUGLE_MARKETDATA_API_KEY
    SHEETS_WRITER_URL
    PORTFOLIO_SHEET_URL
    USER_FEE_RATE (選填,預設 0.001425)
    USER_FEE_MIN (選填,預設 1)
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
from pathlib import Path

# 把 repo 根目錄加進 sys.path,讓我們能 import fugle_agent
# (因為 python scripts/xxx.py 預設只把 scripts/ 加進 path)
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _check_env() -> None:
    required = [
        "ANTHROPIC_API_KEY",
        "FUGLE_MARKETDATA_API_KEY",
        "SHEETS_WRITER_URL",
        "PORTFOLIO_SHEET_URL",
    ]
    missing = [k for k in required if not (os.getenv(k) or "").strip()]
    if missing:
        print(f"❌ 缺環境變數: {missing}")
        sys.exit(1)


def main() -> None:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "technical").lower().strip()
    scope = (sys.argv[2] if len(sys.argv) > 2 else "all").lower().strip()
    if mode not in ("technical", "deep"):
        print(f"❌ 不認識的模式: {mode!r} (要 'technical' 或 'deep')")
        sys.exit(2)
    if scope not in ("all", "positions", "watchlist"):
        print(f"❌ 不認識的範圍: {scope!r} (要 'all' / 'positions' / 'watchlist')")
        sys.exit(2)

    _check_env()
    os.environ.setdefault("FUGLE_MOCK", "0")

    from fugle_agent.tools import organize_all_technical, organize_all_deep

    t0 = datetime.datetime.now()
    print(f"▶️  開始跑 {mode}/{scope} @ {t0.isoformat(timespec='seconds')}")

    args = {"scope": scope}
    if mode == "technical":
        result = asyncio.run(organize_all_technical.handler(args))
    else:
        result = asyncio.run(organize_all_deep.handler(args))

    dt = (datetime.datetime.now() - t0).total_seconds()
    print(f"✅ {mode} 跑完 @ +{dt:.1f}s")

    # 印出簡要結果(便於從 Actions log 追)
    try:
        text = result["content"][0]["text"]
        data = json.loads(text)
        n_pos = data.get("positions", {}).get("n", "?")
        n_wl = data.get("watchlist", {}).get("n", "?")
        print(f"   股票部位: {n_pos} 檔 / 追蹤清單: {n_wl} 檔")
    except Exception as e:
        print(f"   (無法解析結果摘要: {e})")


if __name__ == "__main__":
    main()
