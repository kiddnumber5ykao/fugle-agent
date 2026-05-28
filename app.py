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
        # Portfolio (Google Sheets — 讀)
        "PORTFOLIO_SHEET_URL",
        "PORTFOLIO_POSITIONS_TAB",
        "PORTFOLIO_TRADES_TAB",
        "PORTFOLIO_FUNDS_TAB",
        "PORTFOLIO_FUND_TRADES_TAB",
        # Apps Script Web App URL (寫)
        "SHEETS_WRITER_URL",
        # 個人化偏好(費率、投資風格)— 直接注入 system prompt
        "USER_CONTEXT",
        # 內建工具控制
        "WEB_SEARCH_MAX_USES",
        "MAX_HISTORY_MESSAGES",
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
# page_icon 優先用根目錄的 icon.png / icon.jpg(iPhone 主畫面會用這個);
# 找不到才退回 emoji。
import os.path as _p
_ICON_CANDIDATES = ["icon.png", "icon.jpg", "icon.jpeg"]
_icon_path = next((p for p in _ICON_CANDIDATES if _p.isfile(p)), "📈")

st.set_page_config(
    page_title="加油好嗎",
    page_icon=_icon_path,
    layout="centered",
    initial_sidebar_state="collapsed",   # 預設收起 sidebar(使用者要展開可手動)
)


# ---------------------------------------------------------------------------
# Password gate with remember-me URL token.
#
# 使用者輸對密碼後,我們把 sha256(salt:password)[:16] 塞進 URL 變成 ?auth=XXX。
# 把那個 URL 加到 iPhone 主畫面 / 瀏覽器書籤,以後直接點 → token 在 URL → 自動登入。
# 想撤銷所有舊 token?改 Streamlit Secrets 的 APP_PASSWORD,hash 變了就全部失效。
# ---------------------------------------------------------------------------
def _expected_token(password: str) -> str:
    import hashlib
    return hashlib.sha256(("stark-salt:" + password).encode()).hexdigest()[:16]


def _password_gate() -> bool:
    required = os.environ.get("APP_PASSWORD", "").strip()
    if not required:
        return True  # no password configured → open access

    expected = _expected_token(required)

    # 1) URL 有 ?auth=<token> 且對 → 直接通過(來自主畫面 icon 或書籤)
    url_token = (st.query_params.get("auth") or "").strip()
    if url_token == expected:
        st.session_state.auth_ok = True
        return True

    # 2) session_state 已標記登入 → 通過,並把 token 補進 URL 讓 reload 也認得
    if st.session_state.get("auth_ok"):
        if url_token != expected:
            st.query_params["auth"] = expected
        return True

    # 3) 否則顯示密碼框
    st.title("🔒 加油好嗎")
    with st.form("auth"):
        pwd = st.text_input("輸入存取密碼", type="password")
        ok = st.form_submit_button("進入")
    if ok:
        if pwd == required:
            st.session_state.auth_ok = True
            st.query_params["auth"] = expected
            st.success("✅ 已登入。")
            st.info(
                "📌 **想以後不用再輸密碼?** 看一下現在網址列,後面會多 `?auth=...` 一串。"
                "**把這個含 token 的網址加到 iPhone 主畫面 / 瀏覽器書籤** — "
                "下次點開就直接進來,不會再要求密碼。"
            )
            st.markdown(
                "如果要強制讓所有舊 token 失效(例如不小心把網址貼給別人),"
                "到 Streamlit Secrets 改 `APP_PASSWORD` 就好。"
            )
            st.rerun()
        else:
            st.error("密碼錯誤")
    return False


if not _password_gate():
    st.stop()


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("📈 加油好嗎")
mode_badge = "🎭 mock" if SETTINGS.mock else "📡 live"
st.caption(f"台股研究助理 · {mode_badge} · {MODEL}")


# ---------------------------------------------------------------------------
# Session state — display history (for the UI) + agent history (for the LLM)
#
# 啟動時嘗試從 Google Sheet 隱藏分頁載回上次的對話,讓 app 像一個一直在的 bot。
# ---------------------------------------------------------------------------
def _strip_history_for_persistence(history: list) -> list:
    """把 history 裡的圖片 base64 內容換成文字 placeholder
    (圖片太大塞不進 Sheet cell 的 50K 限制)。"""
    out = []
    for msg in history:
        m = dict(msg)
        content = m.get("content")
        if isinstance(content, list):
            new_blocks = []
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "image":
                    new_blocks.append({
                        "type": "text",
                        "text": "[使用者上傳過一張圖,內容已從紀錄省略]",
                    })
                else:
                    new_blocks.append(blk)
            m["content"] = new_blocks
        out.append(m)
    return out


def _strip_display_for_persistence(display: list) -> list:
    """display 裡的圖片 raw bytes 換成 name 而已。"""
    out = []
    for entry in display:
        e = dict(entry)
        if "images" in e:
            e["images"] = [
                {"name": img.get("name", ""), "persisted": True}
                for img in e["images"]
            ]
        out.append(e)
    return out


def _persist_chat() -> None:
    """每回合結束後呼叫 — 把目前 history + display 存進 Sheet。"""
    try:
        from fugle_agent import sheets_writer as _sw
        _sw.save_history({
            "history": _strip_history_for_persistence(st.session_state.get("history", [])),
            "display": _strip_display_for_persistence(st.session_state.get("display", [])),
        })
    except Exception:
        pass  # 持久化失敗不應該影響 UX


# 一次性:啟動時載入持久化的對話
if "_persisted_loaded" not in st.session_state:
    st.session_state._persisted_loaded = True
    try:
        from fugle_agent import sheets_writer as _sw
        _result = _sw.load_history()
        if _result.get("ok") and _result.get("payload"):
            _payload = _result["payload"] or {}
            st.session_state.history = _payload.get("history", [])
            st.session_state.display = _payload.get("display", [])
    except Exception:
        st.session_state.history = []
        st.session_state.display = []

if "display" not in st.session_state:
    st.session_state.display = []   # [{"role": "user"|"assistant", "content": str, "tools": [...]}]
if "history" not in st.session_state:
    st.session_state.history = []   # passed to the LLM (Anthropic message format)


# ---------------------------------------------------------------------------
# Sidebar(收起預設) — 保留清除對話按鈕,使用者要時可展開
# ---------------------------------------------------------------------------
with st.sidebar:
    if SETTINGS.mock:
        st.info("🎭 Mock 模式")
    else:
        st.success("📡 Live 模式")
    if st.button("🗑️ 清除對話", use_container_width=True):
        st.session_state.display = []
        st.session_state.history = []
        try:
            from fugle_agent import sheets_writer as _sw
            _sw.clear_history()
        except Exception:
            pass
        st.rerun()
    st.caption("⚠️ 僅供示範,不構成投資建議")


# ---------------------------------------------------------------------------
# 頂端狀態列 — 顯示「深度分析完成時間」(從 Sheet 抓最新值)
# ---------------------------------------------------------------------------
def _latest_deep_analysis_time() -> str | None:
    """從股票部位 / 追蹤清單抓最新的「基本面整理時間」。"""
    try:
        from fugle_agent import sheets as _sheets
        latest = None
        for p in (_sheets.load_positions() or []):
            t = str(p.get("基本面整理時間") or p.get("基本面整理时间") or "").strip()
            if t and (latest is None or t > latest):
                latest = t
        for w in (_sheets.load_watchlist() or []):
            t = str(w.get("基本面整理時間") or "").strip()
            if t and (latest is None or t > latest):
                latest = t
        return latest
    except Exception:
        return None


_deep_time = _latest_deep_analysis_time()
_status_col, _btn1_col, _btn2_col = st.columns([2, 1, 1])
with _status_col:
    if _deep_time:
        st.caption(f"📊 深度分析完成:**{_deep_time}**")
    else:
        st.caption("📊 深度分析:**還沒跑過**")
with _btn1_col:
    if st.button("📈 整體技術分析", use_container_width=True,
                  help="抓 K 線、跑 6 個短線訊號、寫回 Sheet。約 10-15 秒。"):
        st.session_state._pending = "整體技術分析"
        st.rerun()
with _btn2_col:
    if st.button("💡 此刻要做什麼", use_container_width=True,
                  help="不重算,只讀 Sheet 上的燈號告訴你該動哪些。"):
        st.session_state._pending = "此刻要做什麼"
        st.rerun()
st.divider()


# ---------------------------------------------------------------------------
# Render existing chat history
# ---------------------------------------------------------------------------
for entry in st.session_state.display:
    with st.chat_message(entry["role"]):
        for tc in entry.get("tools", []):
            with st.expander(f"🔧 工具:`{tc['name']}`", expanded=False):
                st.code(json.dumps(tc["input"], ensure_ascii=False, indent=2), language="json")
        # 重畫使用者貼過的圖片(持久化載回的圖片 data 已 strip,只顯示 placeholder)
        for img in entry.get("images", []):
            if img.get("data"):
                st.image(img["data"], caption=img.get("name", ""))
            else:
                # 持久化載回來,圖片 bytes 已從紀錄省略
                st.caption(f"📸 _{img.get('name', '圖片')}(已從歷史紀錄省略)_")
        if entry.get("content"):
            st.markdown(entry["content"])


# ---------------------------------------------------------------------------
# Input — either from the chat box, or from a clicked example button.
# st.chat_input(accept_file=True) 讓使用者可以拖圖進來。
# ---------------------------------------------------------------------------
prompt_pending = st.session_state.pop("_pending", None)
if prompt_pending is not None:
    # 例題按鈕 — 只有文字
    prompt_text, prompt_files = prompt_pending, []
else:
    try:
        chat_value = st.chat_input(
            "問我台股的事(可拖截圖進來)…",
            accept_file="multiple",
        )
    except TypeError:
        # 老版 Streamlit 不支援 accept_file,退回純文字模式
        chat_value = st.chat_input("問我台股的事…")
    if chat_value is None:
        prompt_text, prompt_files = None, []
    elif isinstance(chat_value, str):
        prompt_text, prompt_files = chat_value, []
    else:
        # ChatInputValue: 有 .text 和 .files
        prompt_text = getattr(chat_value, "text", "") or ""
        prompt_files = list(getattr(chat_value, "files", []) or [])

# 還是要有 prompt 才往下跑
prompt = prompt_text if (prompt_text or prompt_files) else None


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
                    parts = []
                    if event.get("compacted"):
                        parts.append(f"壓縮 {event['compacted']} 筆舊工具結果")
                    if event.get("dropped"):
                        parts.append(f"丟掉 {event['dropped']} 則最舊訊息")
                    if parts:
                        st.caption("♻️ " + "、".join(parts) + "(省 token)")
            elif kind == "history_repaired":
                with tool_log:
                    st.caption(
                        f"🩹 偵測到 {event['fixed']} 筆中斷的工具請求,已自動補上 "
                        "placeholder 讓對話繼續(這些工具的結果遺失,需要再問一次)"
                    )
            elif kind == "max_tokens_truncated":
                with tool_log:
                    st.warning(
                        f"⚠️ 回應被 max_tokens 截斷(目前上限 {event['current_max']} tokens)。"
                        "到 Streamlit Secrets 把 `ANTHROPIC_MAX_TOKENS` 調高(建議 4096 或 8192)。"
                    )
            elif kind == "max_steps_reached":
                text_buffer[0] += f"\n\n_(達到最大 {event['steps']} 步,中止)_"
                text_box.markdown(text_buffer[0])

    asyncio.run(go())
    return text_buffer[0], tools_seen


if prompt is not None or prompt_files:
    import base64

    # 把上傳的圖片轉成 Anthropic image content block + 留一份給 UI 重畫
    image_blocks: list[dict] = []
    image_for_display: list[dict] = []
    for f in prompt_files:
        mime = (getattr(f, "type", None) or "image/png").lower()
        if not mime.startswith("image/"):
            continue
        raw = f.getvalue() if hasattr(f, "getvalue") else f.read()
        b64 = base64.b64encode(raw).decode("utf-8")
        image_blocks.append({
            "type":   "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        })
        image_for_display.append({
            "name": getattr(f, "name", ""),
            "data": raw,
        })

    # Build the user message:
    #   有圖片 → list[block] (image + text)
    #   只有文字 → string
    if image_blocks:
        content_blocks = list(image_blocks)
        if prompt:
            content_blocks.append({"type": "text", "text": prompt})
        agent_input = content_blocks
        display_text = prompt or "(只貼了圖,沒打字)"
    else:
        agent_input = prompt
        display_text = prompt

    # Show user message immediately
    st.session_state.display.append({
        "role":    "user",
        "content": display_text,
        "images":  image_for_display,
    })
    with st.chat_message("user"):
        for img in image_for_display:
            st.image(img["data"], caption=img.get("name", ""))
        st.markdown(display_text)

    # Run agent
    with st.chat_message("assistant"):
        tool_log = st.container()        # tool calls go here, above the text
        text_box = st.empty()            # streaming text replacement target
        try:
            full_text, tools_used = _consume_turn(agent_input, text_box, tool_log)
            text_box.markdown(full_text)
        except Exception as exc:
            # 把常見的暫時性錯誤翻成中文,避免使用者看到 traceback 嚇到
            msg = str(exc)
            etype = type(exc).__name__
            if "overloaded_error" in msg or "529" in msg:
                full_text = (
                    "🚦 **Anthropic 伺服器暫時忙不過來**(529 Overloaded)。\n\n"
                    "這是 Anthropic 那邊的事,不是你的設定問題。**等 10-30 秒按 Enter 重送同一題就好**。\n\n"
                    "高峰時段(美股開盤、Anthropic 發布大新聞)比較常發生。"
                )
            elif "rate_limit_error" in msg or "429" in msg:
                full_text = (
                    "🚧 **你的 API 配額用太兇了**(429 Rate Limit)。\n\n"
                    "等 1 分鐘讓配額重置,或考慮:\n"
                    "- 升 [Anthropic Tier](https://console.anthropic.com/settings/billing)\n"
                    "- 在 Secrets 把 `ANTHROPIC_MODEL` 改成 `claude-sonnet-4-6`(Sonnet 比 Haiku 聰明、call 數少)\n"
                    "- 清空對話從頭來"
                )
            elif "tool_use" in msg and "tool_result" in msg:
                full_text = (
                    "🩹 **對話歷史結構壞了** — 之前某次中斷留下 orphan tool_use。\n\n"
                    "按左邊 sidebar 的「清除對話」按鈕清掉,從新對話開始。"
                )
            elif "timeout" in msg.lower() or "TimeoutError" in etype:
                full_text = (
                    "⏱ **Anthropic 回應太慢逾時** — 通常是 web_search 卡住。\n\n"
                    "重試一次,還是慢的話 Streamlit Secrets 加 `ANTHROPIC_TIMEOUT = \"600\"`。"
                )
            else:
                full_text = f"❌ 出錯了:`{etype}`\n\n```\n{exc}\n```"
            tools_used = []
            text_box.markdown(full_text)

    st.session_state.display.append({
        "role": "assistant",
        "content": full_text,
        "tools": tools_used,
    })

    # 持久化:存進 Sheet 隱藏分頁,下次開 app / 切裝置都能接著聊
    _persist_chat()
