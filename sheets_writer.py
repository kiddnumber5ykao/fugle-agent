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


def manual_sync() -> dict:
    """觸發 Apps Script 跑一次完整同步:
    重建股票部位 + 寫 realized_pnl 到股票交易 + 同步「實際損益」分頁 + 寫目標賣價公式。
    Python 端寫完任何交易應該呼叫這個確保所有衍生 tab 都到位。"""
    return _post("manual_sync", {})


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
