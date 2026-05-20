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
# Older ones get dropped to bound token usage / rate-limit risk.
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "24"))

SYSTEM_PROMPT = """你是「史塔克」— 使用者的個人台股管理助理(命名出自 Tony Stark)。
你的角色是個盡責的私人量化分析師,read-only,絕對不下單。

== 你掌握的工具 ==

【使用者個人資料(從 Google Sheet 讀)】
- get_my_portfolio: 目前持股(代號、股數、平均成本、備註)
- get_trade_log: 完整買賣紀錄(可依 symbol / action 過濾)

【台股市場資料(Fugle Market Data API)】
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
    client = anthropic.AsyncAnthropic(api_key=_api_key())
    tools = _anthropic_tool_specs()
    mode_str = "mock" if SETTINGS.mock else "live"
    system_text = SYSTEM_PROMPT.format(
        mode=mode_str,
        user_context=_user_context(),
        current_time=_now_tw(),
    )

    # Bound history BEFORE appending — keeps the conversation context
    # within token / rate-limit budget even on long sessions.
    dropped = _trim_history_inplace(history, max_messages=MAX_HISTORY_MESSAGES - 2)
    if dropped:
        yield {"type": "history_trimmed", "dropped": dropped}

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
