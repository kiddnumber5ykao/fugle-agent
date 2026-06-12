# 🚀【最新 2026-06-12 · DEPLOY-0612】sheets_writer.py — 寫回 Sheet(bulk_upsert,經 Apps Script)
"""POST helper for the Apps Script Web App that writes to the user's
'加油好嗎' Google Sheet.

The Web App URL lives in the ``SHEETS_WRITER_URL`` env var (set via Streamlit
Secrets).  Without it, all calls return an error envelope explaining how to
set it up.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

WRITER_URL_ENV = "SHEETS_WRITER_URL"


def _post(action: str, args: dict[str, Any], timeout: int = 20) -> dict:
    url = (os.getenv(WRITER_URL_ENV) or "").strip()
    if not url:
        return {
            "ok": False,
            "error": (
                f"環境變數 {WRITER_URL_ENV} 沒設定 — 還沒部署 Apps Script Web App,"
                "或忘了把 Web App URL 加進 Streamlit Cloud Secrets。"
            ),
        }

    payload = json.dumps({"action": action, "args": args}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent":   "fugle-agent/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="ignore")[:500]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {exc.code}: {body or exc.reason}"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Public functions — one per Apps Script action
# ---------------------------------------------------------------------------

def ping() -> dict:
    return _post("ping", {})


def add_trade(**fields) -> dict:
    return _post("add_trade", fields)


def add_fund_trade(**fields) -> dict:
    return _post("add_fund_trade", fields)


def upsert_position(**fields) -> dict:
    return _post("upsert_position", fields)


def bulk_upsert(tab: str, rows: list[dict], key: str = "代號",
                timeout: int = 120) -> dict:
    """一次把多筆 row 寫進指定分頁(批次,最快)。
    Apps Script 端需有 bulk_upsert 動作;沒有的話呼叫端會自動退回逐筆平行。"""
    return _post("bulk_upsert", {"tab": tab, "key": key, "rows": rows},
                 timeout=timeout)


def delete_position(symbol: str) -> dict:
    return _post("delete_position", {"symbol": symbol})


def upsert_fund(**fields) -> dict:
    return _post("upsert_fund", fields)


def delete_fund(fund_id: str) -> dict:
    return _post("delete_fund", {"fund_id": fund_id})


def update_trade_realized(**fields) -> dict:
    """把單筆 SELL 的 realized_pnl 寫回「股票交易」對應的 row。
    必要欄位:date, symbol, action, shares, realized_pnl"""
    return _post("update_trade_realized", fields)


def add_etf_snapshot(**fields) -> dict:
    """在「ETF快照」分頁新增一個持股 row。
    欄位:snapshot_date, etf_symbol, etf_name, stock_symbol, stock_name,
    weight_pct, source, notes"""
    return _post("add_etf_snapshot", fields)


def add_watchlist_item(**fields) -> dict:
    """在「追蹤清單」分頁新增一個追蹤項目。
    欄位:symbol, name, added_date, watch_reason, target_price,
    alert_when, notes"""
    return _post("add_watchlist_item", fields)


def upsert_watchlist_item(**fields) -> dict:
    """新增 / 更新「追蹤清單」分頁(以 symbol 為 key)。
    organize_watchlist 用這個把計算好的 RSI / 距20MA /...等訊號 +
    訊號摘要 + AI 建議寫進既有 row。row 不存在則 append。"""
    return _post("upsert_watchlist_item", fields)


def delete_watchlist_item(symbol: str) -> dict:
    return _post("delete_watchlist_item", {"symbol": symbol})


def cleanup_watchlist(timeout: int = 60) -> dict:
    """整理追蹤清單:① 移除已持有(在股票部位裡)的;② 重複的留第一個;
    ③ 實際損益有、但清單沒有的,補一列(追蹤理由=「曾經」)。完成後照追蹤理由排序。"""
    return _post("cleanup_watchlist", {}, timeout=timeout)


def sort_watchlist(timeout: int = 30) -> dict:
    """把「追蹤清單」資料列照『追蹤理由』排序(同理由排一起)。"""
    return _post("sort_watchlist", {}, timeout=timeout)


# ---- 盤中變化提醒 ----

def log_changes(changes: list[dict], timeout: int = 30) -> dict:
    """記一批「今天的變化」到隱藏分頁(只留今天)。
    changes = [{time, scope, symbol, name, dir, msg}, ...]"""
    return _post("log_changes", {"changes": changes}, timeout=timeout)


def get_changes_today(timeout: int = 20) -> dict:
    """讀今天的變化清單。回傳 {ok, changes: [...]}"""
    return _post("get_changes_today", {}, timeout=timeout)


def save_snapshot(snapshot: str, timeout: int = 20) -> dict:
    """存燈號快照(JSON 字串),給下次比對變化用。"""
    return _post("save_snapshot", {"snapshot": snapshot}, timeout=timeout)


def load_snapshot(timeout: int = 20) -> dict:
    """讀上次的燈號快照。回傳 {ok, snapshot: '...'}"""
    return _post("load_snapshot", {}, timeout=timeout)


def backfill_trade_names(names: dict[str, str], timeout: int = 60) -> dict:
    """把名稱補進「股票交易」名稱空白的 row(以 symbol 比對)。names = {代號: 名稱}。"""
    return _post("backfill_trade_names", {"names": names}, timeout=timeout)


def backfill_realized_names(names: dict[str, str], timeout: int = 60) -> dict:
    """直接把名稱寫進「實際損益」分頁名稱空白的 row(以 symbol 比對)。
    names = {代號: 名稱}。只填空白的,不覆蓋已有名稱。
    這條路不依賴股票部位 / 追蹤清單 — 名字現查現填,獨立補實際損益。"""
    return _post("backfill_realized_names", {"names": names}, timeout=timeout)


def manual_sync(timeout: int = 90) -> dict:
    """觸發 Apps Script 跑一次完整同步:
    重建股票部位 + 寫 realized_pnl 到股票交易 + 同步「實際損益」分頁 + 寫目標賣價公式。
    Python 端寫完任何交易應該呼叫這個確保所有衍生 tab 都到位。

    這個動作牽涉好幾個 tab 寫入 + 公式重算,12+ 部位通常 30-60 秒,給 90 秒 buffer。"""
    return _post("manual_sync", {}, timeout=timeout)


# ---- 對話歷史持久化 ----

def save_history(payload: dict) -> dict:
    """把 {history, display} 整包存到 Google Sheet 隱藏分頁。"""
    return _post("save_history", {"payload": payload})


def load_history() -> dict:
    """從 Google Sheet 隱藏分頁載回上次的對話。
    回傳 {ok, payload: {history, display} | None, saved_at}"""
    return _post("load_history", {})


def clear_history() -> dict:
    """清掉持久化的對話歷史。"""
    return _post("clear_history", {})
