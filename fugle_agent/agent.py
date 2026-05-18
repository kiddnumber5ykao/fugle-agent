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
from typing import AsyncIterator

import anthropic  # type: ignore

from .config import SETTINGS
from .tools import ALL_TOOLS

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_STEPS = int(os.getenv("FUGLE_AGENT_MAX_STEPS", "12"))
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "4096"))

SYSTEM_PROMPT = """你是一位專注於台股的量化研究助理。

你可以使用 Fugle Market Data 工具(read-only — 絕對不會下單):
- get_quote: 即時報價
- get_candles: 日 K 線歷史
- get_intraday_ticks: 盤中逐筆
- get_market_movers: 漲跌幅排行
- compute_indicators: SMA / EMA / RSI
- backtest_sma_crossover, backtest_rsi_mean_reversion: 簡單策略回測

風格指引:
1. 報價先用 get_quote;要分析趨勢時呼叫 get_candles 再算指標。
2. 回測請報告:總報酬、買進持有對照、交易次數、勝率、最大回檔。
3. 沒有把握就再叫一次工具確認 — 不要憑空編造數字。
4. 回測結果務必加上「策略過去表現不代表未來」的提醒。
5. 你目前運行於 {mode} 模式 — mock 模式下的數據是隨機生成的,僅用來示範流程,
   一定要在開頭提醒使用者「以下為 mock 假資料」。
"""


# ---------- tool registry ----------

# Tool name → async handler.  `.handler` exists on both the real SdkMcpTool
# (claude-agent-sdk) and our fallback shim.
_HANDLERS = {t.name: t.handler for t in ALL_TOOLS}


def _anthropic_tool_specs() -> list[dict]:
    """Convert each @tool to Anthropic's tool-use schema."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in ALL_TOOLS
    ]


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
    system_text = SYSTEM_PROMPT.format(mode=mode_str)

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
            if block.type == "text":
                if block.text:
                    yield {"type": "text", "text": block.text}
            elif block.type == "tool_use":
                tool_uses.append(block)
                yield {
                    "type": "tool_call",
                    "name": block.name,
                    "input": block.input or {},
                    "id": block.id,
                }

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
