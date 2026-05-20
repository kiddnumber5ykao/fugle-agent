"""POST helper for the Apps Script Web App that writes to the user's
'史塔克' Google Sheet.

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
