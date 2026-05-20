"""Google Sheets reader — pulls public sheet tabs as CSV.

No OAuth or service account needed; relies on the sheet being shared as
"anyone with the link can view". Each tab is fetched via Google's
visualization (gviz) endpoint, which accepts a tab name directly:

    https://docs.google.com/spreadsheets/d/{ID}/gviz/tq?tqx=out:csv&sheet={TAB}

Env vars
--------
PORTFOLIO_SHEET_URL         Share URL of the spreadsheet (any tab will work)
PORTFOLIO_POSITIONS_TAB     Tab name for current holdings (default: "股票部位")
PORTFOLIO_TRADES_TAB        Tab name for transaction log  (default: "股票交易")
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
FUNDS_TAB_ENV = "PORTFOLIO_FUNDS_TAB"
FUND_TRADES_TAB_ENV = "PORTFOLIO_FUND_TRADES_TAB"

DEFAULT_POSITIONS_TAB = "股票部位"
DEFAULT_TRADES_TAB = "股票交易"
DEFAULT_FUNDS_TAB = "基金部位"
DEFAULT_FUND_TRADES_TAB = "基金交易"


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


def _num_or_none(value: object) -> float | None:
    """Parse a number, but return None for empty/missing values (so the
    caller can tell 'user left it blank' apart from 'user filled 0')."""
    if value is None:
        return None
    s = str(value).replace(",", "").replace("$", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def normalize_holding(row: dict) -> dict | None:
    """Normalize a position row.

    Accepts either ``total_cost`` (preferred, total NTD paid for the
    position) **or** ``cost`` (legacy: per-share cost) and derives the
    other.  Optional ``current_price`` lets the user manually override
    the live Fugle price (e.g. when Fugle is down).
    """
    sym = (row.get("symbol") or row.get("代號") or row.get("ticker") or "")
    sym = str(sym).strip()
    if not sym:
        return None

    shares = int(_num(row.get("shares") or row.get("股數")))

    total_cost = _num(row.get("total_cost") or row.get("總成本"))
    per_share = _num(row.get("cost") or row.get("成本") or row.get("成本價"))

    # Derive whichever is missing.
    if total_cost == 0 and per_share > 0 and shares > 0:
        total_cost = per_share * shares
    if per_share == 0 and total_cost > 0 and shares > 0:
        per_share = total_cost / shares

    return {
        "symbol":         sym,
        "name":           row.get("name") or row.get("名稱") or "",
        "shares":         shares,
        "total_cost":     round(total_cost, 2),
        "cost_per_share": round(per_share, 4),
        "current_price":  _num_or_none(
            row.get("current_price") or row.get("目前價") or row.get("市價")),
        "last_updated":   row.get("last_updated") or row.get("上次更新") or "",
        "notes":          row.get("notes") or row.get("備註") or "",
    }


def normalize_trade(row: dict) -> dict | None:
    sym = row.get("symbol") or row.get("代號") or ""
    sym = str(sym).strip()
    if not sym:
        return None
    action = (row.get("action") or row.get("動作") or "").strip().upper()
    return {
        "date":       row.get("date") or row.get("日期") or "",
        "symbol":     sym,
        "action":     action,
        "shares":     int(_num(row.get("shares") or row.get("股數"))),
        "price":      _num(row.get("price") or row.get("成交價")),
        "fees":       _num(row.get("fees") or row.get("手續費")),
        # 新增:每筆交易的 total_cost(可選),做為 price + fees 的替代填法。
        # BUY 時 = 你實際付的錢;SELL 時 = 你實際收到的錢(都可填,程式自己處理)
        "total_cost": _num(row.get("total_cost") or row.get("總金額")
                           or row.get("成交金額") or row.get("結算金額")),
        "notes":      row.get("notes") or row.get("備註") or "",
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


def normalize_fund(row: dict) -> dict | None:
    """Normalize a fund row — same shape as ``normalize_holding`` but with
    fund-specific naming (fund_id, units, current_nav)."""
    fid = (row.get("fund_id") or row.get("代號")
           or row.get("id") or row.get("symbol") or "")
    fid = str(fid).strip()
    if not fid:
        return None

    units = int(_num(row.get("units") or row.get("單位數")))

    total_cost = _num(row.get("total_cost") or row.get("總成本"))
    per_unit = _num(row.get("avg_cost") or row.get("平均成本") or row.get("cost"))

    if total_cost == 0 and per_unit > 0 and units > 0:
        total_cost = per_unit * units
    if per_unit == 0 and total_cost > 0 and units > 0:
        per_unit = total_cost / units

    return {
        "fund_id":       fid,
        "name":          row.get("name") or row.get("名稱") or "",
        "units":         units,
        "total_cost":    round(total_cost, 2),
        "cost_per_unit": round(per_unit, 4),
        "current_nav":   _num_or_none(
            row.get("current_nav") or row.get("目前NAV") or row.get("目前 NAV")),
        "last_updated":  row.get("last_updated") or row.get("上次更新") or "",
        "notes":         row.get("notes") or row.get("備註") or "",
    }


def load_funds() -> list[dict]:
    tab = os.getenv(FUNDS_TAB_ENV, DEFAULT_FUNDS_TAB)
    rows = fetch_tab(tab)
    if rows and rows[0].get("_error"):
        return rows
    out = [normalize_fund(r) for r in rows]
    return [r for r in out if r is not None]


def normalize_fund_trade(row: dict) -> dict | None:
    """One fund transaction row (parallel to normalize_trade for stocks)."""
    fid = (row.get("fund_id") or row.get("代號") or "").strip()
    if not fid:
        return None
    action = (row.get("action") or row.get("動作") or "").strip().upper()
    return {
        "date":       row.get("date") or row.get("日期") or "",
        "fund_id":    fid,
        "action":     action,
        "units":      int(_num(row.get("units") or row.get("單位數"))),
        "nav":        _num(row.get("nav") or row.get("price") or row.get("成交價")),
        "fees":       _num(row.get("fees") or row.get("手續費")),
        # 同 normalize_trade:支援 total_cost 作為 nav + fees 的替代填法
        "total_cost": _num(row.get("total_cost") or row.get("總金額")
                           or row.get("申購金額") or row.get("贖回金額")),
        "notes":      row.get("notes") or row.get("備註") or "",
    }


def load_fund_trades() -> list[dict]:
    tab = os.getenv(FUND_TRADES_TAB_ENV, DEFAULT_FUND_TRADES_TAB)
    rows = fetch_tab(tab)
    if rows and rows[0].get("_error"):
        return rows
    out = [normalize_fund_trade(r) for r in rows]
    return [r for r in out if r is not None]
