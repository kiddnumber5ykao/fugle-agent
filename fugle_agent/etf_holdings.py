"""台股 ETF 成分股(持股明細)+ 產業配置爬蟲。

資料來源:MoneyDJ 理財網 — 他們對「主動式 ETF」(代號帶 A 那種)也有完整列表,
是目前最穩定的免費來源。

頁面格式(觀察 00981A、0050、00878 後歸納):
- 「持股分佈(依產業)」表:顏色 / 產業 / 投資金額(萬元) / 比例%
- 「持股明細」表:個股名稱 (代號) / 投資比例% / 持有股數
  - 主頁顯示前 10 筆,完整名單在 .../Basic0007B.xdjhtm?etfid=XXX.TW(目前不抓)

公開使用注意:
- 標準 User-Agent,不模擬瀏覽器 JS
- 不高頻打,每次 agent 詢問才抓一次
- 個人使用,沒商業用途
"""

from __future__ import annotations

import re
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

MONEYDJ_URL = "https://www.moneydj.com/etf/x/basic/basic0007.xdjhtm?etfid={code}.tw"


def _http_get(url: str, timeout: int = 20) -> str | None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent":      USER_AGENT,
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer":         "https://www.moneydj.com/etf/",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HTML 表格解析(輕量,不引入 BeautifulSoup)
# ---------------------------------------------------------------------------

def _strip_html(s: str) -> str:
    """剝掉 tags,壓 whitespace。"""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _table_after_label(html: str, label: str) -> str | None:
    """找到 ``label`` 字串在 HTML 出現位置之後的下一張 <table>...</table>。"""
    pos = html.find(label)
    if pos < 0:
        return None
    tbl_start = html.find("<table", pos)
    if tbl_start < 0:
        return None
    tbl_end = html.find("</table>", tbl_start)
    if tbl_end < 0:
        return None
    return html[tbl_start:tbl_end + len("</table>")]


def _extract_rows(table_html: str) -> list[list[str]]:
    """從 <table> HTML 抽出 list of rows,每 row 是 list of cell strings。"""
    rows: list[list[str]] = []
    for row_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL | re.IGNORECASE):
        cells_raw = re.findall(
            r"<t[dh][^>]*>(.*?)</t[dh]>",
            row_match.group(1), re.DOTALL | re.IGNORECASE,
        )
        cells = [_strip_html(c) for c in cells_raw]
        if any(cells):
            rows.append(cells)
    return rows


# ---------------------------------------------------------------------------
# 解析:持股明細 + 產業配置
# ---------------------------------------------------------------------------

def _to_float(s: str) -> float | None:
    s = s.replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_holdings(html: str) -> tuple[list[dict], str | None]:
    """抓「持股明細」表 + 該區塊的「資料日期」。"""
    holdings_date: str | None = None
    # 在「持股明細」字樣後 500 字內找 "資料日期: 2026/05/15"
    pos = html.find("持股明細")
    if pos >= 0:
        m = re.search(r"資料日期[：:]\s*(\d{4}/\d{1,2}/\d{1,2})",
                      html[pos:pos + 500])
        if m:
            holdings_date = m.group(1).replace("/", "-")

    table = _table_after_label(html, "持股明細")
    if not table:
        return [], holdings_date

    holdings: list[dict] = []
    for row in _extract_rows(table):
        if len(row) < 3:
            continue
        name_cell, weight_cell, shares_cell = row[0], row[1], row[2]
        # 「台積電(2330.TW)」 → name="台積電", symbol="2330"
        m = re.match(r"^\s*(.+?)\s*\(\s*([0-9A-Z]+)\.TW\s*\)\s*$", name_cell)
        if not m:
            continue
        name, symbol = m.group(1).strip(), m.group(2).strip()
        weight = _to_float(weight_cell)
        shares = _to_float(shares_cell)
        if weight is None or shares is None:
            continue
        holdings.append({
            "symbol":     symbol,
            "name":       name,
            "weight_pct": round(weight, 4),
            "shares":     int(shares),
        })
    return holdings, holdings_date


def _parse_sectors(html: str) -> list[dict]:
    """產業配置表(顏色 / 產業 / 金額 / 比例)。
    格式:第一欄常是色塊(空字串或單一空白),最後兩欄是金額 + 比例。"""
    # 標題可能是「持股分佈(依產業)」或單純「依產業」,寬鬆 fallback
    for label in ("持股分佈", "依產業"):
        table = _table_after_label(html, label)
        if table:
            break
    else:
        return []

    sectors: list[dict] = []
    for row in _extract_rows(table):
        if len(row) < 3:
            continue
        # 過濾掉「顏色 / 產業 / 投資金額 / 比例(%)」這種 header row
        if any(h in row for h in ("產業", "比例(%)", "投資金額(台幣)")):
            continue
        # 找出所有數字欄
        nums: list[float] = []
        text_cells: list[str] = []
        for c in row:
            v = _to_float(c)
            if v is not None:
                nums.append(v)
            elif c and c != " ":
                text_cells.append(c)
        if len(nums) < 2 or not text_cells:
            continue
        sectors.append({
            "sector":          text_cells[0],
            "amount_10k_ntd":  round(nums[-2], 2),  # 「以萬元為單位」
            "weight_pct":      round(nums[-1], 4),
        })
    return sectors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_holdings(symbol: str) -> dict:
    """主入口:回傳 ETF 的持股 + 產業配置。

    成功:{symbol, source_url, holdings_date, top_holdings, sectors, ...}
    失敗:{symbol, error, source_url}
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "缺少 symbol"}

    url = MONEYDJ_URL.format(code=sym.lower())
    html = _http_get(url)
    if not html:
        return {"symbol": sym, "error": "MoneyDJ 連線失敗", "source_url": url}

    # 主動式 ETF 有時候頁面不存在(404 但 server 回 200 空殼) — 用「持股明細」字樣偵測
    if "持股明細" not in html:
        return {
            "symbol":  sym, "source_url": url,
            "error":   ("MoneyDJ 頁面上找不到「持股明細」區塊 — 該代號可能不是 ETF、"
                        "或 MoneyDJ 不收這檔。可手動到 source_url 確認。"),
        }

    holdings, holdings_date = _parse_holdings(html)
    sectors = _parse_sectors(html)

    out: dict = {
        "symbol":         sym,
        "source_url":     url,
        "holdings_date":  holdings_date,
        "top_holdings":   holdings,
        "n_top":          len(holdings),
        "sectors":        sectors,
        "n_sectors":      len(sectors),
        "note":           "前 10 大持股為 MoneyDJ 主頁顯示。完整持股請至 source_url 看「展開全部」。",
    }
    if not holdings:
        out["warning"] = "解不到持股表 — HTML 結構可能變動,請看 source_url"
    return out
