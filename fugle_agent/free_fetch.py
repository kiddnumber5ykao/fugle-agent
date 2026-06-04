# 📅 ★最新版★ 新增於 2026-06-04  公司基本面改抓免費官方資料(取代付費 web_search)
"""免費官方資料抓取 — 估值 / 月營收 / 新聞,給 _fetch_fundamentals 用。

目的:把最貴的 Anthropic `web_search` 拿掉。改成:
  1) 先用這支模組免費抓好「數字」(估值 / 月營收 / 殖利率)+「新聞標題」,
  2) 再把這些塞進一次「純文字 Haiku」呼叫,產出同樣的白話描述 + 分數 JSON。

資料來源(全部免費、官方 / 公開):
  - 估值(本益比 / 殖利率 / 股價淨值比)→ 證交所每日 BWIBBU_d 開放資料(上市)
  - 月營收(當月 / 年增 / 月增 / 累計年增)→ 證交所 openapi t187ap05_L(上市)+ _O(上櫃)
  - 新聞 → Google News RSS(抓標題)

設計重點:
  - 估值、月營收都是「一次抓整包、用代號查表」→ 整批深度分析時只打 1~2 次網路,
    之後每檔都是 dict 查詢,零額外網路。process 內快取(GitHub Actions 每次 run
    是新 process,等於每次跑都拿最新一份)。
  - 欄位一律用「名稱」比對,**不寫死位置**(證交所偶爾調欄位順序也不會壞)。
  - 全部 best-effort:抓不到就回 None / 空,呼叫端自己降級成「資料不足」。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any, Optional

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0.0.0 Safari/537.36"}

_TIMEOUT = 20


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read()


def _clean_sym(s: Any) -> str:
    return str(s or "").replace("'", "").replace("=", "").strip()


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "--", "N/A", "n/a"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


# ===========================================================================
# 估值 — 證交所每日 BWIBBU_d(上市)
#   欄位(用名稱比對):證券代號 / 本益比 / 殖利率(%) / 股價淨值比
# ===========================================================================

# process 內快取:{symbol: {"per":float|None, "yield":float|None,
#                          "pbr":float|None, "data_date":"YYYY-MM-DD"}}
_valuation_cache: Optional[dict[str, dict]] = None


def _build_valuation_cache() -> dict[str, dict]:
    """抓最近一個有資料的交易日整包 BWIBBU_d,建 {代號: 估值 dict}。"""
    out: dict[str, dict] = {}
    for back in range(0, 8):  # 往前找最多 8 天(跨連假)
        d = date.today() - timedelta(days=back)
        ymd = d.strftime("%Y%m%d")
        url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d"
               f"?date={ymd}&selectType=ALL&response=json")
        try:
            j = json.loads(_http_get(url))
        except Exception:
            continue
        if str(j.get("stat", "")).upper() != "OK":
            continue
        fields = j.get("fields") or []
        data = j.get("data") or []
        if not fields or not data:
            continue

        # 用欄位「名稱」找 index,不寫死位置
        def _idx(*keywords: str) -> Optional[int]:
            for i, f in enumerate(fields):
                fn = str(f)
                if all(k in fn for k in keywords):
                    return i
            return None

        i_sym = _idx("證券代號") if _idx("證券代號") is not None else _idx("代號")
        i_per = _idx("本益比")
        i_yld = _idx("殖利率")
        i_pbr = _idx("股價淨值比") if _idx("股價淨值比") is not None else _idx("淨值比")
        if i_sym is None:
            continue

        data_date = d.strftime("%Y-%m-%d")
        for row in data:
            try:
                sym = _clean_sym(row[i_sym])
            except (IndexError, TypeError):
                continue
            if not sym:
                continue
            out[sym] = {
                "per": _to_float(row[i_per]) if i_per is not None and i_per < len(row) else None,
                "yield": _to_float(row[i_yld]) if i_yld is not None and i_yld < len(row) else None,
                "pbr": _to_float(row[i_pbr]) if i_pbr is not None and i_pbr < len(row) else None,
                "data_date": data_date,
            }
        if out:
            break
    return out


def get_valuation(sym: str) -> dict:
    """回單檔估值 {per, yield, pbr, data_date}。抓不到回各欄 None。"""
    global _valuation_cache
    if _valuation_cache is None:
        try:
            _valuation_cache = _build_valuation_cache()
        except Exception:
            _valuation_cache = {}
    return _valuation_cache.get(_clean_sym(sym),
                                {"per": None, "yield": None, "pbr": None, "data_date": ""})


# ===========================================================================
# 月營收 — 證交所 openapi t187ap05_L(上市) + t187ap05_O(上櫃)
#   每筆 dict,key 用名稱比對:
#     公司代號 / 營業收入-當月營收 /
#     營業收入-去年同月增減(%)=年增 / 營業收入-上月比較增減(%)=月增 /
#     累計營業收入-前期比較增減(%)=累計年增
# ===========================================================================

_revenue_cache: Optional[dict[str, dict]] = None

_REV_ENDPOINTS = [
    "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",   # 上市
    "https://openapi.twse.com.tw/v1/opendata/t187ap05_O",   # 上櫃(待測,失敗自動略過)
]


def _pick(d: dict, *keywords: str) -> Any:
    """在 dict 的 key 裡找「全部 keyword 都包含」的那個 key,回其 value。"""
    for k, v in d.items():
        ks = str(k)
        if all(kw in ks for kw in keywords):
            return v
    return None


def _build_revenue_cache() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for url in _REV_ENDPOINTS:
        try:
            arr = json.loads(_http_get(url))
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        for d in arr:
            if not isinstance(d, dict):
                continue
            sym = _clean_sym(_pick(d, "公司代號") or _pick(d, "代號"))
            if not sym:
                continue
            out[sym] = {
                "month_revenue": _to_float(_pick(d, "當月營收")),
                "yoy": _to_float(_pick(d, "去年同月增減")),       # 年增 %
                "mom": _to_float(_pick(d, "上月比較增減")),       # 月增 %
                "cum_yoy": _to_float(_pick(d, "累計", "前期比較增減")),  # 累計年增 %
                "year_month": str(_pick(d, "資料年月") or _pick(d, "年月") or "").strip(),
                "name": str(_pick(d, "公司名稱") or "").strip(),
            }
    return out


def get_revenue(sym: str) -> dict:
    """回單檔月營收 {month_revenue, yoy, mom, cum_yoy, year_month, name}。"""
    global _revenue_cache
    if _revenue_cache is None:
        try:
            _revenue_cache = _build_revenue_cache()
        except Exception:
            _revenue_cache = {}
    return _revenue_cache.get(_clean_sym(sym),
                              {"month_revenue": None, "yoy": None, "mom": None,
                               "cum_yoy": None, "year_month": "", "name": ""})


# ===========================================================================
# 新聞 — Google News RSS(抓標題)
# ===========================================================================

def get_news_titles(sym: str, name: str = "", limit: int = 6) -> list[str]:
    """逐檔抓 Google News RSS 標題。query 用「名稱 代號」增加命中率。"""
    q = " ".join(x for x in (name, str(sym)) if x).strip() or str(sym)
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(q)
           + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    titles: list[str] = []
    try:
        raw = _http_get(url)
        root = ET.fromstring(raw)
        for item in root.iter("item"):
            t = item.findtext("title")
            if t:
                # Google News 標題常帶「 - 媒體名」後綴,留著也無妨,去頭尾空白即可
                titles.append(t.strip())
            if len(titles) >= limit:
                break
    except Exception:
        # 退一步用 regex 撈 <title>(避免 XML parse 偶發失敗整個沒新聞)
        try:
            txt = raw.decode("utf-8", "ignore")  # type: ignore[has-type]
            found = re.findall(r"<title>(.*?)</title>", txt, re.S)
            # 第 1 個 title 通常是 feed 標題,跳過
            titles = [re.sub(r"<.*?>", "", f).strip() for f in found[1:limit + 1]]
        except Exception:
            titles = []
    return titles


# ===========================================================================
# 一站式:把單檔的免費資料整理成「給 Haiku 的純文字摘要」
# ===========================================================================

def gather_free_facts(sym: str, name: str = "") -> dict:
    """抓單檔的免費估值 + 月營收 + 新聞標題,整理成結構 + 一段純文字。

    回傳:
      {
        "valuation": {...}, "revenue": {...}, "news_titles": [...],
        "data_date": "YYYY-MM-DD",          # 三者裡最新的日期(給 Sheet 顯示)
        "facts_text": "餵給 Haiku 的純文字事實塊",
        "has_any": bool,                     # 是否至少抓到一項數字/新聞
      }
    """
    val = get_valuation(sym)
    rev = get_revenue(sym)
    news = get_news_titles(sym, name)

    # 決定 data_date:估值日期 / 營收年月 / 今天 取最有資訊的
    data_date = val.get("data_date") or ""
    if not data_date and rev.get("year_month"):
        ym = re.sub(r"[^0-9]", "", str(rev["year_month"]))
        if len(ym) >= 6:
            data_date = f"{ym[:4]}-{ym[4:6]}-01"
    if not data_date:
        data_date = date.today().strftime("%Y-%m-%d")

    # 組「事實塊」純文字 — 只放數字事實,描述與評分交給 Haiku
    lines: list[str] = []
    if val.get("per") is not None or val.get("yield") is not None or val.get("pbr") is not None:
        parts = []
        if val.get("per") is not None:
            parts.append(f"本益比 {val['per']:.1f} 倍")
        if val.get("pbr") is not None:
            parts.append(f"股價淨值比 {val['pbr']:.2f} 倍")
        if val.get("yield") is not None:
            parts.append(f"現金殖利率 {val['yield']:.2f}%")
        lines.append("【估值/配息(證交所 " + (val.get("data_date") or "") + ")】"
                     + "、".join(parts))
    else:
        lines.append("【估值/配息】查無官方估值資料(可能是上櫃/興櫃或當日無資料)")

    if any(rev.get(k) is not None for k in ("month_revenue", "yoy", "mom", "cum_yoy")):
        rp = []
        if rev.get("month_revenue") is not None:
            rp.append(f"當月營收 {rev['month_revenue']:.0f} 千元")
        if rev.get("yoy") is not None:
            rp.append(f"年增 {rev['yoy']:+.1f}%")
        if rev.get("mom") is not None:
            rp.append(f"月增 {rev['mom']:+.1f}%")
        if rev.get("cum_yoy") is not None:
            rp.append(f"累計年增 {rev['cum_yoy']:+.1f}%")
        ym = rev.get("year_month") or ""
        lines.append(f"【月營收({ym})】" + "、".join(rp))
    else:
        lines.append("【月營收】查無官方月營收資料")

    if news:
        lines.append("【近期新聞標題】\n" + "\n".join(f"- {t}" for t in news))
    else:
        lines.append("【近期新聞標題】查無新聞")

    has_any = bool(
        val.get("per") is not None or val.get("yield") is not None
        or any(rev.get(k) is not None for k in ("month_revenue", "yoy", "mom", "cum_yoy"))
        or news
    )

    return {
        "valuation": val,
        "revenue": rev,
        "news_titles": news,
        "data_date": data_date,
        "facts_text": "\n".join(lines),
        "has_any": has_any,
    }


# 手動測試:python -m fugle_agent.free_fetch 2330 台積電
if __name__ == "__main__":  # pragma: no cover
    import sys
    _sym = sys.argv[1] if len(sys.argv) > 1 else "2330"
    _name = sys.argv[2] if len(sys.argv) > 2 else ""
    res = gather_free_facts(_sym, _name)
    print(json.dumps({k: v for k, v in res.items() if k != "facts_text"},
                     ensure_ascii=False, indent=2))
    print("---- facts_text ----")
    print(res["facts_text"])
