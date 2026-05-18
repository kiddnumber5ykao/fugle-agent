"""Google Sheets reader — pulls public sheet tabs as CSV.

No OAuth or service account needed; relies on the sheet being shared as
"anyone with the link can view". Each tab is fetched via Google's
visualization (gviz) endpoint, which accepts a tab name directly:

    https://docs.google.com/spreadsheets/d/{ID}/gviz/tq?tqx=out:csv&sheet={TAB}

Env vars
--------
PORTFOLIO_SHEET_URL         Share URL of the spreadsheet (any tab will work)
PORTFOLIO_POSITIONS_TAB     Tab name for current holdings (default: "持股")
PORTFOLIO_TRADES_TAB        Tab name for transaction log  (default: "交易紀錄")
"""

from __future__ import annotations

import csv
import io
import os
import re
import urllib.parse
import urllib.request


SHEET_URL_ENV = "PORTFOLIO_SHEET_URL"
POSITIONS_TAB_ENV = "PORTFOLIO_POSITIONS_TAB"
TRADES_TAB_ENV = "PORTFOLIO_TRADES_TAB"

DEFAULT_POSITIONS_TAB = "持股"
DEFAULT_TRADES_TAB = "交易紀錄"


# ---------------------------------------------------------------------------
# Low-level fetch
# ---------------------------------------------------------------------------

def _extract_sheet_id(url: str) -> str | None:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def _csv_url(sheet_id: str, tab_name: str) -> str:
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/"
        f"gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(tab_name)}"
    )


def fetch_tab(tab_name: str, *, sheet_url: str | None = None,
              timeout: int = 10) -> list[dict]:
    """Fetch one tab as a list of ``{column: value}`` row dicts.

    Returns a single-element list containing ``{"_error": "..."}`` on failure
    so callers can surface the issue without crashing the agent loop.
    """
    sheet_url = (sheet_url or os.getenv(SHEET_URL_ENV, "")).strip()
    if not sheet_url:
        return [{"_error": (
            f"環境變數 {SHEET_URL_ENV} 沒設定 — 請到 Streamlit Cloud Secrets "
            "加入你的 Google Sheet 分享連結。"
        )}]

    sheet_id = _extract_sheet_id(sheet_url)
    if not sheet_id:
        return [{"_error": f"無法從 URL 解出 sheet ID:{sheet_url}"}]

    url = _csv_url(sheet_id, tab_name)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "fugle-agent/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        return [{"_error": (
            f"讀取 tab '{tab_name}' 失敗:{type(exc).__name__}: {exc}。"
            "請確認 Sheet 分享權限為「任何人有連結可檢視」、tab 名稱正確。"
        )}]

    reader = csv.DictReader(io.StringIO(raw))
    rows: list[dict] = []
    for row in reader:
        clean = {(k or "").strip(): (v or "").strip()
                 for k, v in row.items() if k}
        if not any(clean.values()):       # skip totally blank rows
            continue
        rows.append(clean)
    return rows


# ---------------------------------------------------------------------------
# Row normalizers — accept English or Chinese column headers
# ---------------------------------------------------------------------------

def _num(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    s = str(value).replace(",", "").replace("$", "").strip()
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def normalize_holding(row: dict) -> dict | None:
    sym = row.get("symbol") or row.get("代號") or row.get("ticker") or ""
    sym = str(sym).strip()
    if not sym:
        return None
    return {
        "symbol":  sym,
        "name":    row.get("name") or row.get("名稱") or "",
        "shares":  int(_num(row.get("shares") or row.get("股數"))),
        "cost":    _num(row.get("cost") or row.get("成本") or row.get("成本價")),
        "notes":   row.get("notes") or row.get("備註") or "",
    }


def normalize_trade(row: dict) -> dict | None:
    sym = row.get("symbol") or row.get("代號") or ""
    sym = str(sym).strip()
    if not sym:
        return None
    action = (row.get("action") or row.get("動作") or "").strip().upper()
    return {
        "date":    row.get("date") or row.get("日期") or "",
        "symbol":  sym,
        "action":  action,
        "shares":  int(_num(row.get("shares") or row.get("股數"))),
        "price":   _num(row.get("price") or row.get("成交價")),
        "fees":    _num(row.get("fees") or row.get("手續費")),
        "notes":   row.get("notes") or row.get("備註") or "",
    }


# ---------------------------------------------------------------------------
# Public API used by tools
# ---------------------------------------------------------------------------

def load_positions() -> list[dict]:
    tab = os.getenv(POSITIONS_TAB_ENV, DEFAULT_POSITIONS_TAB)
    rows = fetch_tab(tab)
    if rows and rows[0].get("_error"):
        return rows
    out = [normalize_holding(r) for r in rows]
    return [r for r in out if r is not None]


def load_trades() -> list[dict]:
    tab = os.getenv(TRADES_TAB_ENV, DEFAULT_TRADES_TAB)
    rows = fetch_tab(tab)
    if rows and rows[0].get("_error"):
        return rows
    out = [normalize_trade(r) for r in rows]
    return [r for r in out if r is not None]
