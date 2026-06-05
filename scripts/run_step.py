#!/usr/bin/env python3
# 📅 ★最新版★ 上傳於 2026-06-01 22:06  (原最後更新 2026-05-29)(全新:單一步驟,給多 job 平行用)
"""單一步驟入口 — 給 GitHub Actions 多 job 平行跑用。

把「全更新」拆成可平行的步驟,各自一個 job:
    resync       重算交易 + 補名稱(最先,別人都等它)
    technical    技術面(等 resync 完才跑)
    fundamental  基本面(等 resync 完才跑;跟 technical 同時在不同機器跑)
    recompute    重算「我該做啥」(等 technical + fundamental 都完才跑,最後)

用法:python scripts/run_step.py <resync|technical|fundamental|recompute>
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


def _new_watchlist_symbols(time_col: str) -> list[str]:
    """追蹤清單裡『這個步驟還沒做過』的代號(該步驟的資料時間欄空白 = 還沒分析)。
    technical 看「技術資料時間」、fundamental 看「基本面資料時間」— 兩步各看自己的
    欄位,所以平行跑不會互相搶(技術寫完不會讓基本面誤判已做)。"""
    out: list[str] = []
    try:
        from fugle_agent import sheets as _sheets
        for w in (_sheets.load_watchlist() or []):
            if w.get("_error"):
                continue
            sym = str(w.get("symbol") or w.get("代號") or "").strip()
            done = str(w.get(time_col) or "").strip()
            if sym and not done and sym not in out:
                out.append(sym)
    except Exception:
        pass
    return out


def _new_position_symbols(time_col: str) -> list[str]:
    """持股裡『該步驟還沒做過』的代號(資料時間欄空白 = 新持股、還沒分析)。"""
    out: list[str] = []
    try:
        from fugle_agent import sheets as _sheets
        for p in (_sheets.load_positions() or []):
            if p.get("_error"):
                continue
            sym = str(p.get("symbol") or p.get("代號") or "").strip()
            done = str(p.get(time_col) or "").strip()
            if sym and not done and sym not in out:
                out.append(sym)
    except Exception:
        pass
    return out


def main() -> None:
    step = (sys.argv[1] if len(sys.argv) > 1 else "").lower().strip()
    scope = (sys.argv[2] if len(sys.argv) > 2 else "all").lower().strip()
    if step not in ("resync", "technical", "fundamental", "recompute", "foreign", "market"):
        print(f"❌ 不認識的步驟: {step!r}")
        sys.exit(2)
    if scope not in ("all", "positions", "watchlist", "watchlist_new", "positions_new"):
        scope = "all"

    # *_new = 只幫「還沒分析過的新代號」補基本面(省錢):
    #   watchlist_new(新追蹤更新)— 技術 + 基本面都只跑新代號
    #   positions_new(新交易更新)— 技術跑全部持股(免費刷股價),基本面只補新持股
    wl_new = scope == "watchlist_new"
    pos_new = scope == "positions_new"
    eff_scope = ("watchlist" if wl_new else "positions" if pos_new else scope)

    os.environ.setdefault("FUGLE_MOCK", "0")
    from fugle_agent.tools import (resync_and_fill_names, organize_all_technical,
                                   organize_all_deep, _recompute_advice,
                                   detect_intraday_changes)

    t0 = datetime.datetime.now()
    print(f"▶️  步驟 {step}/{scope} 開始 @ {t0.isoformat(timespec='seconds')}")

    if step == "market":
        # 每天一次:名字中文化 + 填上市/上櫃(只改空白/英文名,中文名不動;輕量)
        from fugle_agent.tools import fill_market_labels
        r = fill_market_labels(eff_scope)
        print(f"   ✅ 名字+市場別:部位 {r.get('positions')} / 追蹤 {r.get('watchlist')} 筆有更新")
    elif step == "resync":
        # 依 scope 只動該動的分頁(positions 不碰追蹤清單,反之亦然)
        r = resync_and_fill_names(eff_scope)
        if not r.get("ok"):
            print(f"❌ 重算交易失敗: {r.get('error')}")
            sys.exit(3)
        print(f"   ✅ 補名稱:追蹤 {r.get('watchlist')} / 部位 {r.get('positions')} / "
              f"實際損益 {r.get('realized')}")
    elif step == "technical":
        args = {"scope": eff_scope}
        if wl_new:  # 追蹤:技術只跑新代號
            syms = _new_watchlist_symbols("技術資料時間")
            if not syms:
                print("   ℹ️ 沒有新的追蹤代號,技術面略過"); return
            args["symbols"] = syms
            print(f"   🆕 新追蹤(技術): {syms}")
        # pos_new:技術跑全部持股(免費,刷新所有股價/動能),不加 symbols 篩選
        asyncio.run(organize_all_technical.handler(args))
    elif step == "fundamental":
        args = {"scope": eff_scope}
        if wl_new:
            syms = _new_watchlist_symbols("基本面資料時間")
            if not syms:
                print("   ℹ️ 沒有新的追蹤代號,基本面略過"); return
            args["symbols"] = syms
            print(f"   🆕 新追蹤(基本面): {syms}")
        elif pos_new:  # 新交易:基本面只補沒分析過的新持股
            syms = _new_position_symbols("基本面資料時間")
            if not syms:
                print("   ℹ️ 沒有新持股要補基本面,基本面略過"); return
            args["symbols"] = syms
            print(f"   🆕 新持股(基本面): {syms}")
        asyncio.run(organize_all_deep.handler(args))
    elif step == "recompute":
        adv = _recompute_advice(eff_scope)
        print(f"   🧭 部位 {adv.get('positions')} / 追蹤 {adv.get('watchlist')}")
        # 最後比對「上次的燈」抓今天的變化(免費,純比對)
        detect_intraday_changes(eff_scope)
    elif step == "foreign":
        # 外資每日(證交所/櫃買官方 EOD)→ 籌碼面燈號 + 法人籌碼 + 翻轉提醒。免費。
        from fugle_agent.foreign_flow import update_foreign
        r = update_foreign(eff_scope)
        if not r.get("ok"):
            print(f"❌ 外資更新失敗: {r.get('error')}")
            sys.exit(3)
        # 外資改了籌碼面燈 → 重算一次「我該做啥」,再抓翻燈變化
        adv = _recompute_advice(eff_scope)
        print(f"   🧭 重算 部位 {adv.get('positions')} / 追蹤 {adv.get('watchlist')}")
        detect_intraday_changes(eff_scope)

    dt = (datetime.datetime.now() - t0).total_seconds()
    print(f"✅ 步驟 {step} 完成 @ +{dt:.1f}s")


if __name__ == "__main__":
    main()
