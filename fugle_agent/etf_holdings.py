"""台股 ETF 成分股 + 產業配置爬蟲(多來源 fallback)。

來源優先順序:
1. MoneyDJ — 前 10 大持股 + 產業分布(顯示金額單位:萬元)
2. wantgoo.com — 完整 50 檔持股 + 每檔現價 / 近期漲跌(資料較深)

兩邊都打,哪邊有資料就用哪邊。Streamlit Cloud 的 IP 偶爾會被 MoneyDJ 擋,
wantgoo 比較友善。

公開使用注意:標準 UA、單次查詢才打、個人用途。
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

MONEYDJ_URL = "https://www.moneydj.com/etf/x/basic/basic0007.xdjhtm?etfid={code}.tw"
WANTGOO_URL = "https://www.wantgoo.com/stock/etf/{code}/constituent"


def _http_get(url: str, *, timeout: int = 20,
              referer: str | None = None) -> tuple[str | None, str]:
    """回傳 (html, error_note)。成功時 error_note 為 ""。"""
    headers = {
        "User-Agent":      USER_AGENT,
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            html = resp.read().decode("utf-8", errors="ignore")
            return html, ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 輕量 HTML 解析 helpers
# ---------------------------------------------------------------------------

def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _table_after_label(html: str, label: str) -> str | None:
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
    rows: list[list[str]] = []
    for row_match in re.finditer(r"<tr[^>]*>(.*?)</tr>",
                                 table_html, re.DOTALL | re.IGNORECASE):
        cells_raw = re.findall(
            r"<t[dh][^>]*>(.*?)</t[dh]>",
            row_match.group(1), re.DOTALL | re.IGNORECASE,
        )
        cells = [_strip_html(c) for c in cells_raw]
        if any(cells):
            rows.append(cells)
    return rows


def _to_float(s: str) -> float | None:
    if s is None:
        return None
    s = s.replace(",", "").strip()
    if not s or s in ("-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 來源 1:MoneyDJ
# ---------------------------------------------------------------------------

def _fetch_moneydj(symbol: str) -> dict:
    url = MONEYDJ_URL.format(code=symbol.lower())
    html, err = _http_get(url, referer="https://www.moneydj.com/etf/")
    if html is None:
        return {"source": "moneydj", "source_url": url, "error": err}
    if "持股明細" not in html:
        return {"source": "moneydj", "source_url": url,
                "error": "頁面找不到「持股明細」區塊(該代號可能不在 MoneyDJ)"}

    # 持股明細
    holdings_date = None
    pos = html.find("持股明細")
    if pos >= 0:
        m = re.search(r"資料日期[：:]\s*(\d{4}/\d{1,2}/\d{1,2})",
                      html[pos:pos + 500])
        if m:
            holdings_date = m.group(1).replace("/", "-")

    holdings: list[dict] = []
    table = _table_after_label(html, "持股明細")
    if table:
        for row in _extract_rows(table):
            if len(row) < 3:
                continue
            m = re.match(r"^\s*(.+?)\s*\(\s*([0-9A-Z]+)\.TW\s*\)\s*$", row[0])
            if not m:
                continue
            name, sym = m.group(1).strip(), m.group(2).strip()
            weight = _to_float(row[1])
            shares = _to_float(row[2])
            if weight is None or shares is None:
                continue
            holdings.append({
                "symbol":     sym,
                "name":       name,
                "weight_pct": round(weight, 4),
                "shares":     int(shares),
            })

    # 產業配置
    sectors: list[dict] = []
    for label in ("持股分佈", "依產業"):
        st = _table_after_label(html, label)
        if not st:
            continue
        for row in _extract_rows(st):
            if any(h in row for h in ("產業", "比例(%)", "投資金額(台幣)")):
                continue
            nums = [v for v in (_to_float(c) for c in row) if v is not None]
            text = [c for c in row if _to_float(c) is None and c.strip() and c != " "]
            if len(nums) < 2 or not text:
                continue
            sectors.append({
                "sector":         text[0],
                "amount_10k_ntd": round(nums[-2], 2),
                "weight_pct":     round(nums[-1], 4),
            })
        if sectors:
            break

    return {
        "source":        "moneydj",
        "source_url":    url,
        "holdings_date": holdings_date,
        "top_holdings":  holdings,
        "sectors":       sectors,
    }


# ---------------------------------------------------------------------------
# 來源 2:wantgoo
# ---------------------------------------------------------------------------

def _fetch_wantgoo(symbol: str) -> dict:
    url = WANTGOO_URL.format(code=symbol.lower())
    html, err = _http_get(url, referer="https://www.wantgoo.com/stock/etf/")
    if html is None:
        return {"source": "wantgoo", "source_url": url, "error": err}

    holdings_date = None
    # Wantgoo 頁面常有「Q1基金行業比重」「Q1ETF持股分布」這種季別標題
    # 取 meta-description 抓資料日期
    m = re.search(r"(\d{4}/\d{1,2}/\d{1,2})持股佔比", html)
    if m:
        holdings_date = m.group(1).replace("/", "-")

    holdings: list[dict] = []
    # 找含「ETF持股分布」標籤後的表格
    table = _table_after_label(html, "ETF持股分布")
    if not table:
        # fallback:任意「成分股」表
        table = _table_after_label(html, "成分股")
    if table:
        for row in _extract_rows(table):
            if len(row) < 3:
                continue
            # 第一欄應該是代號(4-6 位英數字)
            sym_raw = row[0].strip()
            # 跳過 header rows("代號" / "成分股" / "比例")
            if sym_raw in ("代號", "成分股", "比例", "") or "比例" in sym_raw:
                continue
            # 抽出純代號(濾掉前後空白 / 連結文字)
            m = re.match(r"^([0-9A-Z]{4,6})", sym_raw)
            if not m:
                continue
            sym = m.group(1)
            name = row[1].strip() if len(row) > 1 else ""
            # name 可能帶 "*" 或 "-KY" 後綴,保留
            weight = _to_float(row[2]) if len(row) > 2 else None
            change = _to_float(row[3]) if len(row) > 3 else None
            price  = _to_float(row[4]) if len(row) > 4 else None
            if weight is None or not name:
                continue
            holdings.append({
                "symbol":         sym,
                "name":           name,
                "weight_pct":     round(weight, 4),
                "weight_change":  change,
                "current_price":  price,
            })

    # 行業比重 — wantgoo 一列塞 3 個產業(產業/比例/增減 × 3)
    sectors: list[dict] = []
    st = _table_after_label(html, "基金行業比重")
    if st:
        for row in _extract_rows(st):
            i = 0
            while i + 1 < len(row):
                name = row[i].strip()
                weight = _to_float(row[i + 1]) if i + 1 < len(row) else None
                if name and name not in ("產業名稱",) and weight is not None and weight > 0:
                    sectors.append({
                        "sector":     name,
                        "weight_pct": round(weight, 4),
                    })
                i += 3

    return {
        "source":        "wantgoo",
        "source_url":    url,
        "holdings_date": holdings_date,
        "top_holdings":  holdings,
        "sectors":       sectors,
    }


# ---------------------------------------------------------------------------
# 主入口:多來源 fallback
# ---------------------------------------------------------------------------

def fetch_holdings(symbol: str) -> dict:
    """先試 MoneyDJ,再試 wantgoo,選哪邊抓到的資料多就用哪邊。

    若兩邊都掛,回傳含兩邊的錯誤訊息讓使用者 debug。"""
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "缺少 symbol"}

    attempts: list[dict] = []

    md = _fetch_moneydj(sym)
    attempts.append({
        "source":   md["source"],
        "url":      md["source_url"],
        "ok":       not md.get("error") and bool(md.get("top_holdings")),
        "error":    md.get("error"),
        "n_holdings": len(md.get("top_holdings") or []),
    })

    wg = _fetch_wantgoo(sym)
    attempts.append({
        "source":     wg["source"],
        "url":        wg["source_url"],
        "ok":         not wg.get("error") and bool(wg.get("top_holdings")),
        "error":      wg.get("error"),
        "n_holdings": len(wg.get("top_holdings") or []),
    })

    # 選持股數多的那邊(wantgoo 通常 ≥ 30 筆,MoneyDJ 只有 10 筆)
    candidates = [r for r in [md, wg] if r.get("top_holdings")]
    if not candidates:
        return {
            "symbol":          sym,
            "error":           "所有爬蟲來源都被擋 — Streamlit Cloud IP 常被金融類網站封殺",
            "attempts":        attempts,
            "fallback_links":  [md["source_url"], wg["source_url"]],
            "try_web_search":  True,
            "web_search_hint": (
                f"請用 web_search 查「{sym} 持股 成分股 最新」,"
                f"從搜尋結果摘要出前 10 大持股 + 比例。"
                "Anthropic 的 web_search 用他們自己的 IP,不會被擋。"
            ),
        }

    best = max(candidates, key=lambda r: len(r["top_holdings"]))
    best["symbol"] = sym
    best["attempts"] = attempts
    return best
