#!/usr/bin/env python3
# 📅 最後更新:2026-05-29(加 symbols + 最後重算我該做啥)
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

    # 第 3 個參數(選填):只跑這幾檔代號,逗號分隔,例如 "6902,2330"
    symbols_raw = (sys.argv[3] if len(sys.argv) > 3 else "").strip()
    symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]

    _check_env()
    os.environ.setdefault("FUGLE_MOCK", "0")

    from fugle_agent.tools import organize_all_technical, organize_all_deep

    t0 = datetime.datetime.now()
    _sym_note = f" symbols={symbols}" if symbols else ""
    print(f"▶️  開始跑 {mode}/{scope}{_sym_note} @ {t0.isoformat(timespec='seconds')}")

    args = {"scope": scope}
    if symbols:
        args["symbols"] = symbols
    handler = organize_all_technical.handler if mode == "technical" else organize_all_deep.handler
    result = asyncio.run(handler(args))

    dt = (datetime.datetime.now() - t0).total_seconds()
    print(f"✅ {mode} 跑完 @ +{dt:.1f}s")

    # 解析結果 + 自動重跑失敗的股票(只重試 1 次,避免無窮迴圈)
    failed_syms: list[str] = []
    try:
        text = result["content"][0]["text"]
        data = json.loads(text)
        pos = data.get("positions") or {}
        wl  = data.get("watchlist") or {}
        n_pos = pos.get("n", "?")
        n_wl  = wl.get("n", "?")
        print(f"   股票部位: {n_pos} 檔 / 追蹤清單: {n_wl} 檔")
        for r in (pos.get("items") or []):
            if r.get("error"):
                failed_syms.append(r["symbol"])
        for r in (wl.get("items") or []):
            if r.get("error"):
                if r["symbol"] not in failed_syms:
                    failed_syms.append(r["symbol"])
    except Exception as e:
        print(f"   (無法解析結果摘要: {e})")

    if failed_syms:
        print(f"♻️ 自動重跑 {len(failed_syms)} 檔失敗的: {failed_syms}")
        t1 = datetime.datetime.now()
        retry_result = asyncio.run(handler({**args, "symbols": failed_syms}))
        dt2 = (datetime.datetime.now() - t1).total_seconds()
        print(f"   重跑跑完 @ +{dt2:.1f}s")
        # 印重跑後還剩幾檔失敗
        try:
            rdata = json.loads(retry_result["content"][0]["text"])
            still_failed: list[str] = []
            for r in ((rdata.get("positions") or {}).get("items") or []):
                if r.get("error"):
                    still_failed.append(r["symbol"])
            for r in ((rdata.get("watchlist") or {}).get("items") or []):
                if r.get("error") and r["symbol"] not in still_failed:
                    still_failed.append(r["symbol"])
            if still_failed:
                print(f"⚠️ 重跑後仍失敗: {still_failed}")
            else:
                print(f"✅ 重跑全部成功!")
        except Exception:
            pass

    # 【最後一步】讀齊 4 個燈號,重算「我該做啥」— 確保它永遠最後才定稿
    try:
        from fugle_agent.tools import _recompute_advice
        adv = _recompute_advice(scope)
        print(f"🧭 重算我該做啥:部位 {adv.get('positions')} 筆 / "
              f"追蹤 {adv.get('watchlist')} 筆")
    except Exception as e:
        print(f"⚠️ 重算我該做啥失敗: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
