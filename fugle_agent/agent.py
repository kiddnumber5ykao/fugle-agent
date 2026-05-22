"""Anthropic-powered agent — uses the `anthropic` Python SDK directly.

Two entry points:
    * ``run_turn_streaming(user_input, history)`` — async generator that
      yields fine-grained events (text chunks, tool calls, tool results).
      Mutates ``history`` in place so multi-turn conversations work.
    * ``run_once(prompt)`` — CLI helper that prints those events to stdout.

Why not `claude-agent-sdk`?  Because that one requires the `claude` CLI to be
installed and logged-in locally.  Using the regular Anthropic SDK keeps the
setup to a single ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import anthropic  # type: ignore

from .config import SETTINGS
from .tools import ALL_TOOLS

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_STEPS = int(os.getenv("FUGLE_AGENT_MAX_STEPS", "12"))
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "4096"))
# Sliding window: keep at most this many recent messages in the LLM context.
# Older ones get *compacted* first(tool_result 大塊 JSON 換成短 stub),
# 只有再超出才會真的丟掉,讓上下文記憶比舊版深 2-3 倍。
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "60"))
# 最近 N 個「新 user 訊息」的 tool_result 保留完整,更舊的會被 compact。
COMPACT_KEEP_RECENT_TURNS = int(os.getenv("COMPACT_KEEP_RECENT_TURNS", "6"))
# Anthropic SDK 預設 60s 容易在 web_search 多跳時超時,拉長 + 加重試。
ANTHROPIC_TIMEOUT = float(os.getenv("ANTHROPIC_TIMEOUT", "300"))
ANTHROPIC_MAX_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "2"))

SYSTEM_PROMPT = """你是「史塔克」— 使用者的個人台股管理助理(命名出自 Tony Stark)。
你的角色是個盡責的私人量化分析師,read-only,絕對不下單。

⚠️ **語言規則(最高優先級)**:
**永遠用繁體中文(台灣用語)回應**。**絕對不要用任何簡體字**。
例如要寫「臺積電 / 台積電」不要寫「台积电」、要寫「資產」不要寫「资产」、
「現金」不要「现金」、「損益」不要「损益」。即使使用者用簡體字提問,
你也用繁體中文回。專有名詞用台灣慣用譯名(軟體不用軟件、伺服器不用服務器)。

== 你掌握的工具 ==

【使用者個人資料(從 Google Sheet 讀)】
- get_my_portfolio: 目前持股,每筆含 symbol / shares / total_cost / cost_per_share /
  current_price(手動兜底,可能 null) / notes
- get_trade_log: 完整買賣紀錄(可依 symbol / action 過濾)
- get_my_funds: 目前持有的基金,每筆含 fund_id / units / total_cost / cost_per_unit /
  current_nav(手動兜底,可能 null) / notes
- get_fund_trade_log: 完整基金買賣紀錄(可依 fund_id / action 過濾)
- get_fund_nav: 從鉅亨網 cnyes 即時抓單一基金 NAV(best-effort,失敗就用 current_nav)

【寫入工具 — 自動更新 Sheet】
- log_stock_trade: 使用者說「我買了/賣了 X 股 Y @ Z」時呼叫。會自動:
  (1) 在「股票交易」加一行 (2) 在「股票部位」加總或扣減該股部位(加權平均)
  (3) SELL 時回傳已實現損益。**呼叫前先用今日日期(從上方「時間」拿)
  跟使用者確認所有欄位再執行**。
- log_fund_trade: 基金版本的同個工具
- rebuild_positions_from_trades: **從「股票交易」歷史重建「股票部位」**(初始化情境)
- rebuild_funds_from_trades: 同上,基金版
- backfill_position_names: **回補「股票部位」中空白的 name 欄**(用內建表查)
- backfill_fund_names: **回補「基金部位」中空白的 name 欄**(從 cnyes 抓,best-effort)
- valuate_portfolio: **估算所有股票部位的現值、扣完手續費 + 證交稅的淨損益**(僅算,不改部位)。
  ETF 自動套 0.1% 稅、一般股 0.3% 稅、手續費套 USER_FEE_RATE(預設 0.1425% / 下限 NT$1)。
  使用者問「我現在賺多少」「我的部位現在值多少」「全賣會剩多少」→ **直接呼叫這個**,
  不要自己心算,把回傳的明細整理成 Markdown 表格給使用者。
- sync_portfolio_from_trades: ⭐ **最強一鍵工具** — 重建部位 + 補名字 + 抓即時價 + 算估值 + 寫回 Sheet 全套。
  使用者說「我剛加了交易」「幫我重新整理」「重新計算所有東西」「同步一下我的部位」→ **直接呼叫這個**,
  不要分兩步 rebuild + valuate。
- get_realized_pnl: **算「已實現損益」** — 過去 SELL 的累積賺賠、勝率、每檔加總。
  使用者問「我交易賺多少」「我賣掉賺多少」「我的勝率多少」「今年交易績效」→ **直接呼叫這個**。
  支援日期區間(from_date / to_date)跟單一代號(symbol)過濾。
  **看到「把每筆 SELL 記到 Sheet」「回填賺賠到交易紀錄」這類話 → 帶 write_back=true**,
  它會把每筆 SELL 的 realized_pnl 寫回「股票交易」對應 row。
  把回傳整理成 Markdown 表格,先講總額,再列每檔細節。
- ping_sheets_writer: 測 Apps Script Web App 是否設好

⚠️ **重要:寫入流程**
看到使用者輸入「我剛買了/賣了 ...」這類陳述句:
1. 先**回顯**你準備寫的所有欄位(日期、代號、動作、股數、價格、手續費、稅、備註)
2. 問使用者「確認嗎?」
3. 使用者說「確認」「OK」「好」「ok」之類後才呼叫 log_stock_trade
4. 寫入後**清楚告知**寫了什麼、新部位狀態、SELL 的話加上已實現損益

⚠️ **持股 / 基金成本邏輯**(超重要,常算錯):
- `total_cost` = **整筆部位你實際付的總金額**(NTD)
- `cost_per_share` / `cost_per_unit` = 自動算的「每股 / 每單位」單價
- 算市值:現價 × shares(或 units)
- 算未實現損益:市值 − total_cost
- 算損益%:(市值 − total_cost) / total_cost × 100
- **絕對不要拿 cost_per_share × shares 重新算 total_cost**,直接用 total_cost 就對了

⚠️ **取現價優先順序**:
- 持股:get_quote(Fugle 即時)→ 失敗才看 `current_price` 欄位(使用者手動填的)
- 基金:get_fund_nav(cnyes 即時)→ 失敗才看 `current_nav` 欄位

【台股市場資料(Fugle Market Data API)】
- search_taiwan_symbol: **依公司名稱查代號** — 使用者沒給代號、只給名字
  (「台積電」「玉山金」「半導體 ETF」)時,**一定先呼叫這個**確認代號,
  不要憑記憶猜,常會猜錯到同名公司(玉山金 vs 玉山銀;群創 vs 群益)。
- get_quote: 台股即時報價 — 代號是 4 位數字(2330、0050、2454)
- get_candles: 台股日 K 線歷史
- get_intraday_ticks: 台股盤中逐筆
- get_market_movers: 台股漲跌幅排行

【美股 / 全球市場資料(Yahoo Finance,延遲 15-20 分鐘)】
- get_us_quote: 美股 / ETF / 加密貨幣報價 — 代號用英文(AAPL、SPY、BTC-USD)
- get_us_candles: 美股日 K 線歷史

【新聞】
- get_stock_news: **個股**新聞 — 美股直接打代號(AAPL),台股加 .TW 後綴(2330.TW)
- web_search: **總體 / 政策 / 跨股票** 新聞與資訊搜尋(中英文都可),
  例如「央行升息」「美國通膨數據」「半導體景氣」「ASML 財報」

【分析】
- compute_indicators: SMA / EMA / RSI
- backtest_sma_crossover, backtest_rsi_mean_reversion: 策略回測

⚠️ 工具選擇規則:
- 看到 4 位數字代號(2330)→ 台股,用 get_quote / get_candles
- 看到英文代號(AAPL)→ 美股,用 get_us_quote / get_us_candles
- **看到中文公司名沒給代號**(「台積電」「玉山金」「中信金」「鴻海」…)
  → **必先 search_taiwan_symbol 確認代號** → 再用 get_quote 等工具。
  禁止憑記憶猜代號,常會把「玉山金」打成「玉山銀」、「群益」「群創」也常混。
- 個股新聞 → get_stock_news(台股要加 .TW)
- 總體 / 政策 / 「市場現在怎麼了」→ web_search

== 風格指引 ==
1. 使用者問「我的持股」「我的損益」「我買的」等個人化問題時,先叫 get_my_portfolio
   或 get_trade_log,**不要憑空編造**。
2. 算市值 / 未實現損益:get_my_portfolio 拿持股 → 對每檔 get_quote → 自己算
   (市值 = 現價 × 股數;損益 = 市值 − 成本×股數;損益% = 損益 / (成本×股數))。
3. 算「淨損益」(扣完手續費 / 證交稅後的實際金額)時,**務必使用下方「使用者個人設定」
   裡的費率**,不要自己編。如果使用者沒設,套用台股預設(0.1425% / 0.3%)並提醒一次。
4. 算已實現損益(賣出的部分):get_trade_log 拿紀錄,配對 BUY / SELL 用先進先出(FIFO)。
5. 回測請報告:總報酬、買進持有對照、交易次數、勝率、最大回檔。
6. 沒把握就再叫一次工具確認 — 寧可多查也不要編。
7. 回測結果、策略建議務必加上「過去績效不代表未來,不構成投資建議」。
8. 整理數字盡量用 Markdown 表格,容易讀。金額顯示加千分位逗號。

== 環境 ==
你目前運行於 {mode} 模式 — mock 模式下的市場資料是隨機生成的,僅供示範,
請在開頭明確提醒「以下為 mock 假資料」。Live 模式 (📡) 才是真實 Fugle 行情。

== 時間 ==
**現在時間:{current_time}**
- 台股盤中:週一至週五 09:00 – 13:30(台北時間)
- 台股盤後 / 週末 / 國定假日:Fugle 報價會是「上個交易日的收盤價」
- 美股盤中(換算台北時間):
  - 夏令時間(3 月~11 月初):週一至週五 21:30 – 翌日 04:00
  - 冬令時間(11 月初~3 月):週一至週五 22:30 – 翌日 05:00
- 看到「今天」「現在」「最近」等詞,**用上面那個時間判斷**,不要憑想像

== 使用者個人設定 ==
{user_context}

開場時不用自我介紹,直接幫忙就好。
"""


def _user_context() -> str:
    """讀 USER_CONTEXT 環境變數;沒設就回一段預設的提醒。"""
    raw = (os.getenv("USER_CONTEXT") or "").strip()
    if raw:
        return raw
    return ("(使用者尚未在 Streamlit Secrets 設定 USER_CONTEXT。"
            "計算淨損益時請套用台股預設:"
            "買進手續費 0.1425%、賣出手續費 0.1425%、賣出證交稅 0.3%。"
            "並提醒使用者可在 Streamlit Secrets 加上 USER_CONTEXT 來指定個人費率。)")


_WEEKDAY_TW = ["一", "二", "三", "四", "五", "六", "日"]


def _now_tw() -> str:
    """目前的台北時間,含星期、盤中 / 盤後判斷。"""
    utc_now = datetime.now(timezone.utc)
    tw_now = utc_now.astimezone(timezone(timedelta(hours=8)))
    weekday_ch = _WEEKDAY_TW[tw_now.weekday()]
    is_weekday = tw_now.weekday() < 5
    h, m = tw_now.hour, tw_now.minute
    in_session = is_weekday and (
        (h == 9) or (10 <= h < 13) or (h == 13 and m <= 30)
    )
    status = "📈 台股盤中" if in_session else (
        "🌙 台股盤後 / 收盤" if is_weekday else "🛌 週末或假日,台股沒開盤"
    )
    return (
        f"{tw_now.strftime('%Y-%m-%d')} 星期{weekday_ch} "
        f"{tw_now.strftime('%H:%M')}(台北時間,UTC+8){status}"
    )


# ---------- tool registry ----------

# Tool name → async handler.  `.handler` exists on both the real SdkMcpTool
# (claude-agent-sdk) and our fallback shim.
_HANDLERS = {t.name: t.handler for t in ALL_TOOLS}


WEB_SEARCH_MAX_USES = int(os.getenv("WEB_SEARCH_MAX_USES", "5"))


def _anthropic_tool_specs() -> list[dict]:
    """Custom (client-side) tools + Anthropic-managed server tools (web_search)."""
    specs: list[dict] = [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in ALL_TOOLS
    ]
    # Anthropic-managed server tool: web search.  Claude can browse the web on
    # its own and get results back without us writing any handler.
    specs.append({
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": WEB_SEARCH_MAX_USES,
    })
    return specs


async def _run_tool(name: str, args: dict) -> str:
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)
    try:
        envelope = await handler(args)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    if isinstance(envelope, dict) and "content" in envelope:
        return envelope["content"][0].get("text", "")
    return json.dumps(envelope, ensure_ascii=False, default=str)


def _api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "找不到 ANTHROPIC_API_KEY — 請到 https://console.anthropic.com 申請,"
            "然後 export ANTHROPIC_API_KEY=... 後重試。"
        )
    return key


def _blocks_to_dicts(blocks) -> list[dict]:
    """Convert Anthropic content blocks to plain dicts so they survive
    json-serialization and Streamlit session-state round-trips."""
    out = []
    for b in blocks:
        if hasattr(b, "model_dump"):
            out.append(b.model_dump())
        else:
            out.append(b)
    return out


def _compact_old_tool_results(history: list, *, keep_recent_turns: int,
                              stub_max_chars: int = 400) -> int:
    """把舊訊息裡的 tool_result 大塊內容換成短 stub,只保留前 ``stub_max_chars`` 字。

    為什麼這樣做:工具結果(尤其 get_my_portfolio / get_candles / web_search)
    動輒 5-50 KB JSON,token 吃超兇。對話脈絡其實只需要 user 訊息 + assistant
    的回答文字,舊工具結果換成短 stub 之後 agent 依然知道「我那時呼叫過什麼、
    結果大致長什麼樣」,需要時可以再叫一次工具。

    我們用「新 user 訊息」(content 是 str)當對話回合分界 — 最近
    ``keep_recent_turns`` 個回合的 tool_result 保留完整,更舊的才 compact。

    Returns 壓縮的 tool_result block 數量(供 UI 顯示)。
    """
    # 找出所有「新 user 訊息」的位置 — 這代表一個新對話回合的開始
    fresh_user_indices = [
        i for i, m in enumerate(history)
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    if len(fresh_user_indices) <= keep_recent_turns:
        return 0
    # 第一個「要保留」的新 user 訊息的 index — 在它之前的全部都 compact
    cutoff = fresh_user_indices[-keep_recent_turns]

    compacted = 0
    for i in range(cutoff):
        msg = history[i]
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") != "tool_result":
                continue
            raw = blk.get("content")
            # tool_result 的 content 有兩種格式:純字串、或 [{"type":"text","text":"..."}]
            if isinstance(raw, str):
                if len(raw) > stub_max_chars:
                    blk["content"] = (
                        raw[:stub_max_chars]
                        + f"...(舊工具結果省略,原長 {len(raw)} 字元)"
                    )
                    compacted += 1
            elif isinstance(raw, list):
                for sub in raw:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        text = sub.get("text", "")
                        if len(text) > stub_max_chars:
                            sub["text"] = (
                                text[:stub_max_chars]
                                + f"...(舊工具結果省略,原長 {len(text)} 字元)"
                            )
                            compacted += 1
    return compacted


def _trim_history_inplace(history: list, *, max_messages: int) -> int:
    """Drop oldest messages to keep history within ``max_messages``.

    Important constraint: a turn that started a tool call (``tool_use``)
    *must* be followed by its ``tool_result`` in the next user message — if
    we cut between them, the Anthropic API rejects the request.  So we only
    cut at the boundary of a *fresh* user message (one whose content is a
    plain string, not a list of ``tool_result`` blocks).

    Returns the number of messages dropped (for logging / UI).
    """
    if len(history) <= max_messages:
        return 0

    cutoff = len(history) - max_messages
    # Walk forward from the desired cutoff looking for a clean boundary.
    cut_at = None
    for i in range(cutoff, len(history)):
        msg = history[i]
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            cut_at = i
            break

    if cut_at is None or cut_at <= 0:
        return 0  # no safe cut point found; leave as-is

    dropped = cut_at
    del history[:cut_at]
    return dropped


# ---------- main loop ----------

async def run_turn_streaming(user_input: str, history: list) -> AsyncIterator[dict]:
    """Run one conversational turn, yielding events for each meaningful unit
    of work.  ``history`` is mutated in place so subsequent turns get context.

    Event shapes:
        {"type": "text", "text": str}
        {"type": "tool_call", "name": str, "input": dict, "id": str}
        {"type": "tool_result", "name": str, "result": str}
        {"type": "max_steps_reached", "steps": int}
    """
    client = anthropic.AsyncAnthropic(
        api_key=_api_key(),
        timeout=ANTHROPIC_TIMEOUT,
        max_retries=ANTHROPIC_MAX_RETRIES,
    )
    tools = _anthropic_tool_specs()
    mode_str = "mock" if SETTINGS.mock else "live"
    system_text = SYSTEM_PROMPT.format(
        mode=mode_str,
        user_context=_user_context(),
        current_time=_now_tw(),
    )

    # 兩段式縮減上下文:
    # (1) Compact 舊工具結果(留前 400 字 stub) — 大幅省 token,對話脈絡保留
    # (2) 若仍超出 MAX_HISTORY_MESSAGES,才從前面切掉訊息
    compacted = _compact_old_tool_results(
        history, keep_recent_turns=COMPACT_KEEP_RECENT_TURNS)
    dropped = _trim_history_inplace(history, max_messages=MAX_HISTORY_MESSAGES - 2)
    if compacted or dropped:
        yield {"type": "history_trimmed", "dropped": dropped, "compacted": compacted}

    history.append({"role": "user", "content": user_input})

    for _step in range(MAX_STEPS):
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_text,
            tools=tools,
            messages=history,
        )

        tool_uses = []
        for block in response.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                if block.text:
                    yield {"type": "text", "text": block.text}
            elif btype == "tool_use":
                # client-side custom tool — we need to execute it
                tool_uses.append(block)
                yield {
                    "type": "tool_call",
                    "name": block.name,
                    "input": block.input or {},
                    "id": block.id,
                }
            elif btype == "server_tool_use":
                # Anthropic-managed tool (e.g. web_search) — already running
                # server-side, we just surface to the UI for transparency.
                yield {
                    "type": "tool_call",
                    "name": f"🌐 {getattr(block, 'name', 'web_search')}",
                    "input": getattr(block, "input", {}) or {},
                    "id":    getattr(block, "id", ""),
                }
            elif btype == "web_search_tool_result":
                # Search results are auto-injected into Claude's next thinking,
                # we don't need to handle them ourselves.
                pass

        # Persist this assistant turn (text + any tool_use blocks) to history
        history.append({"role": "assistant", "content": _blocks_to_dicts(response.content)})

        # No more tool calls → conversation turn done.
        if response.stop_reason != "tool_use" or not tool_uses:
            return

        # Execute tools and feed results back as the next user turn.
        results: list[dict] = []
        for tu in tool_uses:
            result_text = await _run_tool(tu.name, tu.input or {})
            yield {"type": "tool_result", "name": tu.name, "result": result_text}
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result_text,
            })
        history.append({"role": "user", "content": results})

    yield {"type": "max_steps_reached", "steps": MAX_STEPS}


# ---------- CLI ----------

async def run_once(prompt: str) -> str:
    """One-shot CLI run.  Fresh history each call."""
    mode_str = "mock" if SETTINGS.mock else "live"
    print(f"[mode={mode_str}, model={MODEL}]\n")

    history: list = []
    final_text: list[str] = []
    async for event in run_turn_streaming(prompt, history):
        kind = event["type"]
        if kind == "text":
            print(event["text"], end="", flush=True)
            final_text.append(event["text"])
        elif kind == "tool_call":
            args = json.dumps(event["input"], ensure_ascii=False)
            print(f"\n  ↳ [tool] {event['name']}({args})", flush=True)
        elif kind == "max_steps_reached":
            print(f"\n[達到最大 {event['steps']} 步,中止]")
    print()
    return "".join(final_text)


def _cli() -> None:
    if len(sys.argv) < 2:
        print('Usage: python -m fugle_agent.agent "<你的提問>"', file=sys.stderr)
        sys.exit(2)
    asyncio.run(run_once(" ".join(sys.argv[1:])))


if __name__ == "__main__":
    _cli()
