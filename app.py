"""Streamlit chat UI for the Fugle agent.

Run locally:    streamlit run app.py
Deploy:         push to GitHub, then connect at https://share.streamlit.io
"""

from __future__ import annotations

import asyncio
import json
import os

import streamlit as st


# ---------------------------------------------------------------------------
# Bridge Streamlit secrets → os.environ BEFORE importing fugle_agent (so
# Settings reads them correctly during import).
# ---------------------------------------------------------------------------
def _load_secrets_into_env() -> None:
    keys = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_MAX_TOKENS",
        "FUGLE_MARKETDATA_API_KEY",
        "FUGLE_MOCK",
        "FUGLE_RATE_LIMIT_SLEEP",
        "FUGLE_AGENT_MAX_STEPS",
        "APP_PASSWORD",
        # Portfolio (Google Sheets)
        "PORTFOLIO_SHEET_URL",
        "PORTFOLIO_POSITIONS_TAB",
        "PORTFOLIO_TRADES_TAB",
        # 個人化偏好(費率、投資風格)— 直接注入 system prompt
        "USER_CONTEXT",
    )
    try:
        for k in keys:
            if k in st.secrets and not os.environ.get(k):
                os.environ[k] = str(st.secrets[k])
    except Exception:
        # secrets.toml doesn't exist (e.g. local dev without it) — that's fine.
        pass


_load_secrets_into_env()

from fugle_agent.agent import run_turn_streaming, MODEL  # noqa: E402
from fugle_agent.config import SETTINGS  # noqa: E402


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Fugle Agent · 台股研究助理",
    page_icon="📈",
    layout="centered",
    initial_sidebar_state="auto",
)


# ---------------------------------------------------------------------------
# Lightweight password gate (optional — set APP_PASSWORD in secrets to enable)
# ---------------------------------------------------------------------------
def _password_gate() -> bool:
    required = os.environ.get("APP_PASSWORD", "").strip()
    if not required:
        return True  # no password configured → open access
    if st.session_state.get("auth_ok"):
        return True
    st.title("🔒 Fugle Agent")
    with st.form("auth"):
        pwd = st.text_input("輸入存取密碼", type="password")
        ok = st.form_submit_button("進入")
    if ok:
        if pwd == required:
            st.session_state.auth_ok = True
            st.rerun()
        else:
            st.error("密碼錯誤")
    return False


if not _password_gate():
    st.stop()


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("📈 Fugle Agent")
mode_badge = "🎭 mock" if SETTINGS.mock else "📡 live"
st.caption(f"台股研究助理 · {mode_badge} · {MODEL}")


# ---------------------------------------------------------------------------
# Session state — display history (for the UI) + agent history (for the LLM)
# ---------------------------------------------------------------------------
if "display" not in st.session_state:
    st.session_state.display = []   # [{"role": "user"|"assistant", "content": str, "tools": [...]}]
if "history" not in st.session_state:
    st.session_state.history = []   # passed to the LLM (Anthropic message format)


# ---------------------------------------------------------------------------
# Sidebar — settings + examples
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ 設定")
    if SETTINGS.mock:
        st.info(
            "🎭 **Mock 模式** — 資料是 SHA1 種子產生的假數據,僅供示範。"
            "想接真實資料,在 Streamlit secrets 加上 `FUGLE_MARKETDATA_API_KEY` 後重新部署。"
        )
    else:
        st.success("📡 **Live 模式** — Fugle 真實資料")

    # --- 對話控制 ---
    n_msgs = len(st.session_state.get("history", []))
    st.caption(f"📜 對話訊息數:**{n_msgs}** 則")
    if n_msgs >= 20:
        st.warning("對話有點長,舊訊息會自動被丟掉以省 token 額度。")

    if st.button(
        "🗑️ 清除對話",
        use_container_width=True,
        help="只清掉這個瀏覽器分頁裡的對話歷史。Streamlit Secrets 裡的 "
             "USER_CONTEXT、API key、Google Sheet 連結都會保留。",
    ):
        st.session_state.display = []
        st.session_state.history = []
        st.rerun()

    st.divider()
    st.caption("💡 範例問題(點一下直接問):")
    examples = [
        "今天 2330 報價如何?",
        "拉 0050 過去 90 天 K 線,算 20/60 SMA,現在是黃金交叉還死亡交叉?",
        "跑 2330 從 2025-01-01 到今天的 20/60 SMA 交叉回測,跟買進持有比較",
        "比較 2330 跟 2454 過去 60 天的 RSI 14,哪個比較弱?",
        "今天漲幅前 3 大的股票",
    ]
    for ex in examples:
        if st.button(ex, key=f"ex_{hash(ex) & 0xFFFF}", use_container_width=True):
            st.session_state._pending = ex
            st.rerun()

    st.divider()
    st.caption(
        "⚠️ 本工具僅供研究示範,**不構成投資建議**;策略回測過去績效不代表未來。"
    )


# ---------------------------------------------------------------------------
# Render existing chat history
# ---------------------------------------------------------------------------
for entry in st.session_state.display:
    with st.chat_message(entry["role"]):
        for tc in entry.get("tools", []):
            with st.expander(f"🔧 工具:`{tc['name']}`", expanded=False):
                st.code(json.dumps(tc["input"], ensure_ascii=False, indent=2), language="json")
        if entry.get("content"):
            st.markdown(entry["content"])


# ---------------------------------------------------------------------------
# Input — either from the chat box, or from a clicked example button
# ---------------------------------------------------------------------------
prompt = st.session_state.pop("_pending", None) or st.chat_input("問我台股的事…")


def _consume_turn(prompt: str, text_box, tool_log):
    """Drive the async generator from sync code so Streamlit can update
    placeholders mid-stream."""
    text_buffer = [""]
    tools_seen: list[dict] = []

    async def go():
        async for event in run_turn_streaming(prompt, st.session_state.history):
            kind = event["type"]
            if kind == "text":
                text_buffer[0] += event["text"]
                text_box.markdown(text_buffer[0] + " ▌")
            elif kind == "tool_call":
                tools_seen.append({"name": event["name"], "input": event["input"]})
                with tool_log:
                    with st.expander(f"🔧 工具:`{event['name']}`", expanded=False):
                        st.code(
                            json.dumps(event["input"], ensure_ascii=False, indent=2),
                            language="json",
                        )
            elif kind == "history_trimmed":
                with tool_log:
                    st.caption(f"♻️ 已自動丟掉 {event['dropped']} 則舊訊息以省 token")
            elif kind == "max_steps_reached":
                text_buffer[0] += f"\n\n_(達到最大 {event['steps']} 步,中止)_"
                text_box.markdown(text_buffer[0])

    asyncio.run(go())
    return text_buffer[0], tools_seen


if prompt:
    # Show user message immediately
    st.session_state.display.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Run agent
    with st.chat_message("assistant"):
        tool_log = st.container()        # tool calls go here, above the text
        text_box = st.empty()            # streaming text replacement target
        try:
            full_text, tools_used = _consume_turn(prompt, text_box, tool_log)
            text_box.markdown(full_text)
        except Exception as exc:
            full_text = f"❌ 出錯了:`{type(exc).__name__}`\n\n```\n{exc}\n```"
            tools_used = []
            text_box.markdown(full_text)

    st.session_state.display.append({
        "role": "assistant",
        "content": full_text,
        "tools": tools_used,
    })
