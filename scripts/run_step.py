#!/usr/bin/env python3
# 📅 ★最新版★ 上傳於 2026-05-31 20:54  (原最後更新 2026-05-29)(全新:單一步驟,給多 job 平行用)
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


def main() -> None:
    step = (sys.argv[1] if len(sys.argv) > 1 else "").lower().strip()
    scope = (sys.argv[2] if len(sys.argv) > 2 else "all").lower().strip()
    if step not in ("resync", "technical", "fundamental", "recompute"):
        print(f"❌ 不認識的步驟: {step!r}")
        sys.exit(2)
    if scope not in ("all", "positions", "watchlist"):
        scope = "all"

    os.environ.setdefault("FUGLE_MOCK", "0")
    from fugle_agent.tools import (resync_and_fill_names, organize_all_technical,
                                   organize_all_deep, _recompute_advice)

    t0 = datetime.datetime.now()
    print(f"▶️  步驟 {step}/{scope} 開始 @ {t0.isoformat(timespec='seconds')}")

    if step == "resync":
        # 依 scope 只動該動的分頁(positions 不碰追蹤清單,反之亦然)
        r = resync_and_fill_names(scope)
        if not r.get("ok"):
            print(f"❌ 重算交易失敗: {r.get('error')}")
            sys.exit(3)
        print(f"   ✅ 補名稱:追蹤 {r.get('watchlist')} / 部位 {r.get('positions')} / "
              f"實際損益 {r.get('realized')}")
    elif step == "technical":
        asyncio.run(organize_all_technical.handler({"scope": scope}))
    elif step == "fundamental":
        asyncio.run(organize_all_deep.handler({"scope": scope}))
    elif step == "recompute":
        adv = _recompute_advice(scope)
        print(f"   🧭 部位 {adv.get('positions')} / 追蹤 {adv.get('watchlist')}")

    dt = (datetime.datetime.now() - t0).total_seconds()
    print(f"✅ 步驟 {step} 完成 @ +{dt:.1f}s")


if __name__ == "__main__":
    main()
