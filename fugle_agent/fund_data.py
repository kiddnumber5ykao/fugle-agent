"""Mutual fund NAV scraper — best-effort fetch from cnyes / Anue 鉅亨網.

cnyes 沒提供公開 API,所以我們爬他們的網頁。多種 URL pattern 都試一次,
解析嵌在 HTML 裡的 JSON state 或常見 DOM 結構。如果某天 cnyes 改版打不到,
agent 仍可從使用者 Google Sheet 的「目前 NAV」欄位讀手動值。

公開使用注意:
- 用標準 User-Agent,**不**模擬瀏覽器 JS
- 不高頻打,每次 agent 詢問才抓一次
- 個人用,沒商業用途
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# cnyes 的基金頁面有多種代號格式 — T101.001、A12345、ISIN(LU0079474960)等。
# 我們不挑剔,就拿使用者填的字串直接套 URL。
_DETAIL_URLS = [
    "https://fund.cnyes.com/detail/{fund_id}/Performance",
    "https://fund.cnyes.com/detail/{fund_id}",
    "https://fund.cnyes.com/detail/{fund_id}/Profile",
]


# ---------------------------------------------------------------------------
# Low-level HTTP fetch
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 20) -> str | None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent":      USER_AGENT,
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer":         "https://fund.cnyes.com/",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Parsing — multiple patterns, try each
# ---------------------------------------------------------------------------

_NAV_KEY_NAMES = {
    # 英文 key 慣例
    "nav", "navValue", "navPrice", "currentNav", "latestNav",
    "closeNav", "unitPrice",
    # 中文 key(少見)
    "淨值", "目前淨值", "最新淨值",
}


def _walk(obj: Any, target_keys: set[str]) -> Any:
    """DFS 找 dict 裡符合 target_keys 的值(且為數字)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in target_keys and isinstance(v, (int, float)) and v > 0:
                return v
            found = _walk(v, target_keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _walk(item, target_keys)
            if found is not None:
                return found
    return None


def _parse(html: str) -> tuple[float | None, str | None, str | None]:
    """Try multiple patterns. Returns (nav, name, asOf) — any of which may be None."""
    nav: float | None = None
    name: str | None = None
    as_of: str | None = None

    # Pattern A: Next.js <script id="__NEXT_DATA__"> 內嵌的 JSON state
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    if m:
        try:
            data = json.loads(m.group(1))
            found = _walk(data, _NAV_KEY_NAMES)
            if found is not None:
                nav = float(found)
            # 順便抓基金名稱
            name = _walk(data, {"fundName", "name", "title", "中文名稱"})
        except Exception:
            pass

    # Pattern B: 「淨值」字樣後面跟著的數字
    if nav is None:
        m = re.search(r'(?:淨值|NAV)[^\d]{0,15}([0-9]{1,5}\.[0-9]{2,4})', html)
        if m:
            nav = float(m.group(1))

    # Pattern C: data-nav="X" 或 data-value="X"
    if nav is None:
        m = re.search(r'data-(?:nav|value)\s*=\s*"([0-9]+\.[0-9]+)"', html)
        if m:
            nav = float(m.group(1))

    # Pattern D: 任何「日期 + 淨值 + 數字」的組合
    if as_of is None:
        m = re.search(r'(20\d{2}[-/]\d{1,2}[-/]\d{1,2})', html)
        if m:
            as_of = m.group(1).replace("/", "-")

    # 名稱備援:從 <title> 抽
    if name is None:
        m = re.search(r"<title>([^<]+)</title>", html)
        if m:
            name = m.group(1).split("|")[0].strip()

    return nav, name, as_of


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_nav(fund_id: str) -> dict:
    """抓取單一基金的即時 NAV。

    成功:回傳 {fund_id, nav, name, asOf, source, fetched_at}
    失敗:回傳 {fund_id, error}
    """
    fund_id = (fund_id or "").strip()
    if not fund_id:
        return {"error": "缺少 fund_id"}

    last_err: str | None = None
    for tmpl in _DETAIL_URLS:
        url = tmpl.format(fund_id=urllib.parse.quote(fund_id, safe=""))
        html = _http_get(url)
        if not html:
            last_err = f"HTTP failed for {url}"
            continue
        nav, name, as_of = _parse(html)
        if nav is not None:
            return {
                "fund_id":    fund_id,
                "nav":        round(nav, 4),
                "name":       name,
                "asOf":       as_of,
                "source":     url,
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
        last_err = f"could not parse NAV from {url}"

    return {
        "fund_id": fund_id,
        "error": (
            f"無法從 cnyes 抓到 NAV({last_err})。"
            "可能原因:基金代號錯誤、cnyes 改版、Streamlit Cloud IP 被擋。"
            "請改用 Sheet「基金部位」分頁的「current_nav」欄位手動填值兜底。"
        ),
    }
