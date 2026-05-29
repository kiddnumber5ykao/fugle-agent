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
        # GitHub Actions 觸發按鈕用
        "GITHUB_PAT",
        "GITHUB_REPO",
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
    initial_sidebar_state="auto",   # sidebar 預設展開,放分析按鈕
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
# Helper:從追蹤清單抓最新「技術整理時間」+「基本面整理時間」
# ---------------------------------------------------------------------------
def _latest_analysis_times() -> dict:
    """讀股票部位 + 追蹤清單分別的「技術整理時間」+「基本面整理時間」。
    回傳 {pos_tech, pos_deep, wl_tech, wl_deep}。"""
    out = {"pos_tech": None, "pos_deep": None, "wl_tech": None, "wl_deep": None}
    try:
        from fugle_agent import sheets as _sheets

        def _max(cur, t):
            t = str(t or "").strip()
            if not t:
                return cur
            if cur is None or t > cur:
                return t
            return cur

        # 股票部位:normalize_holding 會 strip 自訂欄位,改直接 fetch_tab
        try:
            tab = os.environ.get("PORTFOLIO_POSITIONS_TAB", _sheets.DEFAULT_POSITIONS_TAB)
            for p in (_sheets.fetch_tab(tab) or []):
                if p.get("_error"):
                    continue
                out["pos_tech"] = _max(out["pos_tech"], p.get("技術整理時間"))
                out["pos_deep"] = _max(out["pos_deep"], p.get("基本面整理時間"))
        except Exception:
            pass

        # 追蹤清單:本來就 raw row
        for w in (_sheets.load_watchlist() or []):
            if w.get("_error"):
                continue
            out["wl_tech"] = _max(out["wl_tech"], w.get("技術整理時間"))
            out["wl_deep"] = _max(out["wl_deep"], w.get("基本面整理時間"))
    except Exception:
        pass
    return out


# 每個按鈕狀態 — 儲存在 session_state
def _mark_job_started(key: str, est_seconds: int) -> None:
    import datetime as _dt
    if "job_states" not in st.session_state:
        st.session_state.job_states = {}
    st.session_state.job_states[key] = {
        "started":  _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))),
        "est_secs": est_seconds,
    }


def _job_indicator(key: str) -> str:
    """根據 session_state 算出按鈕後綴指示:🔄 跑中 / ✅ 跑完(估計)。"""
    import datetime as _dt
    job = st.session_state.get("job_states", {}).get(key)
    if not job:
        return ""
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
    elapsed = (now - job["started"]).total_seconds()
    if elapsed < job["est_secs"]:
        return " 🔄"
    return " ✅"


# ---------------------------------------------------------------------------
# Helper:終止所有正在跑的 GitHub Actions runs
# ---------------------------------------------------------------------------
def _cancel_all_running_workflows() -> dict:
    """打 GitHub API 取消所有 status=in_progress 或 queued 的 run。"""
    import json as _json
    import urllib.error
    import urllib.request

    pat = (os.environ.get("GITHUB_PAT") or "").strip()
    repo = (os.environ.get("GITHUB_REPO") or "kiddnumber5ykao/fugle-agent").strip()
    if not pat:
        return {"ok": False, "error": "沒設 GITHUB_PAT"}

    headers = {
        "Accept":               "application/vnd.github+json",
        "Authorization":        f"Bearer {pat}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent":           "fugle-agent/0.1",
    }
    cancelled: list = []
    errors: list = []
    for status in ("in_progress", "queued"):
        try:
            list_url = (f"https://api.github.com/repos/{repo}/actions/runs"
                        f"?status={status}&per_page=30")
            req = urllib.request.Request(list_url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                runs_data = _json.loads(resp.read().decode("utf-8"))
            for run in (runs_data.get("workflow_runs") or []):
                run_id = run.get("id")
                if not run_id:
                    continue
                cancel_url = (f"https://api.github.com/repos/{repo}/actions"
                              f"/runs/{run_id}/cancel")
                creq = urllib.request.Request(cancel_url, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(creq, timeout=10) as cresp:
                        if cresp.status in (200, 202):
                            cancelled.append({
                                "id":   run_id,
                                "name": run.get("name", ""),
                                "status": status,
                            })
                except urllib.error.HTTPError as ce:
                    errors.append(f"run {run_id}: HTTP {ce.code}")
        except Exception as e:
            errors.append(f"list {status}: {type(e).__name__}: {e}")

    # 同時清掉 session_state 的 job indicators (狀態歸零)
    if "job_states" in st.session_state:
        st.session_state.job_states = {}

    return {
        "ok":         True,
        "cancelled":  cancelled,
        "n_cancelled": len(cancelled),
        "errors":     errors,
    }


# ---------------------------------------------------------------------------
# Helper:透過 GitHub Actions API 觸發背景 workflow
# ---------------------------------------------------------------------------
def _trigger_github_workflow(workflow_file: str,
                              inputs: dict | None = None) -> dict:
    """POST 到 GitHub Actions workflow_dispatch endpoint,讓 workflow 在 GitHub
    那邊跑(完全不佔 Streamlit 資源,使用者可以繼續聊天)。

    需要 Streamlit Secrets 設定:
        GITHUB_PAT — fine-grained PAT,scope 至少要包含 Actions read+write
        GITHUB_REPO — 例如 "kiddnumber5ykao/fugle-agent"(可選)
    """
    import json as _json
    import urllib.error
    import urllib.request

    pat = (os.environ.get("GITHUB_PAT") or "").strip()
    repo = (os.environ.get("GITHUB_REPO") or "kiddnumber5ykao/fugle-agent").strip()
    if not pat:
        return {"ok": False, "error": "Streamlit Secrets 沒設 GITHUB_PAT — "
                                       "請到 GitHub 建一個 fine-grained token 加進去"}

    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"{workflow_file}/dispatches")
    body: dict = {"ref": "main"}
    if inputs:
        body["inputs"] = {k: str(v) for k, v in inputs.items()}
    req = urllib.request.Request(
        url,
        data=_json.dumps(body).encode("utf-8"),
        headers={
            "Accept":               "application/vnd.github+json",
            "Authorization":        f"Bearer {pat}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type":         "application/json",
            "User-Agent":           "fugle-agent/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            # GitHub 回 204 No Content 代表成功觸發
            return {"ok": resp.status in (200, 204), "status": resp.status}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {body or e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Sidebar — 分析按鈕 + 狀態列 + 對話控制
# ---------------------------------------------------------------------------
with st.sidebar:
    st.subheader("🚀 一鍵分析")
    st.caption("**📈 技術分析**(約 1-2 分鐘)")
    _tc1, _tc2 = st.columns(2)
    with _tc1:
        _label = f"部位{_job_indicator('tech_pos')}"
        if st.button(_label, key="btn_tech_pos", use_container_width=True):
            res = _trigger_github_workflow("intraday_technical.yml",
                                            inputs={"scope": "positions"})
            if res.get("ok"):
                _mark_job_started("tech_pos", 180)
                st.success("✅ 部位技術分析已觸發")
                st.rerun()
            else:
                st.error(f"❌ {res.get('error')}")
    with _tc2:
        _label = f"追蹤清單{_job_indicator('tech_wl')}"
        if st.button(_label, key="btn_tech_wl", use_container_width=True):
            res = _trigger_github_workflow("intraday_technical.yml",
                                            inputs={"scope": "watchlist"})
            if res.get("ok"):
                _mark_job_started("tech_wl", 180)
                st.success("✅ 追蹤清單技術分析已觸發")
                st.rerun()
            else:
                st.error(f"❌ {res.get('error')}")

    st.caption("**💎 深度分析**(約 5-10 分鐘)")
    _dc1, _dc2 = st.columns(2)
    with _dc1:
        _label = f"部位{_job_indicator('deep_pos')}"
        if st.button(_label, key="btn_deep_pos", use_container_width=True):
            res = _trigger_github_workflow("daily_deep_analysis.yml",
                                            inputs={"scope": "positions"})
            if res.get("ok"):
                _mark_job_started("deep_pos", 600)
                st.success("✅ 部位深度分析已觸發")
                st.rerun()
            else:
                st.error(f"❌ {res.get('error')}")
    with _dc2:
        _label = f"追蹤清單{_job_indicator('deep_wl')}"
        if st.button(_label, key="btn_deep_wl", use_container_width=True):
            res = _trigger_github_workflow("daily_deep_analysis.yml",
                                            inputs={"scope": "watchlist"})
            if res.get("ok"):
                _mark_job_started("deep_wl", 600)
                st.success("✅ 追蹤清單深度分析已觸發")
                st.rerun()
            else:
                st.error(f"❌ {res.get('error')}")

    _resync_label = f"📋 重算交易+補名稱{_job_indicator('pos_resync')}"
    if st.button(_resync_label, use_container_width=True,
                  help="剛在股票交易加/改/刪交易後按這個 — 從交易表重算股票部位、實際損益、"
                       "5/10/15/20% 目標賣價公式,並**幫股票部位 + 追蹤清單**補上空白的股票名稱。"
                       "(其他 4 個分析按鈕不會補名稱,專心填技術 / 基本面欄位) 30-60 秒。"):
        _mark_job_started("pos_resync", 90)
        from fugle_agent import sheets_writer as _sw
        from fugle_agent import sheets as _sh
        # Step 1: 跑 Apps Script 同步
        sync_res = {"ok": False}
        with st.spinner("⏳ 步驟 1/2:重算部位、損益、目標賣價公式…"):
            try:
                sync_res = _sw.manual_sync()
            except Exception as e:
                sync_res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if not sync_res.get("ok"):
            st.error(f"❌ Step 1 失敗:{sync_res.get('error')}")
        else:
            # Step 2: 補空白名稱(同時補股票部位 + 股票交易)
            n_filled_pos = 0
            n_filled_trades = 0
            with st.spinner("⏳ 步驟 2/2:補上空白的股票名稱…"):
                try:
                    from fugle_agent.tools import _lookup_stock_name
                    # 2a) 股票部位的空白名稱
                    name_cache: dict[str, str] = {}
                    for p in (_sh.load_positions() or []):
                        if p.get("_error"):
                            continue
                        sym = str(p.get("symbol") or "").strip()
                        cur_name = str(p.get("name") or "").strip()
                        if sym and not cur_name:
                            new_name = name_cache.get(sym) or _lookup_stock_name(sym)
                            if new_name:
                                name_cache[sym] = new_name
                                wb = _sw.upsert_position(
                                    symbol=sym, 代號=sym,
                                    name=new_name, 名稱=new_name)
                                if wb.get("ok"):
                                    n_filled_pos += 1
                    # 2b) 股票交易裡空白的名稱(暴力做法:重發 add_trade 沒辦法,
                    # 改用 Apps Script 一個新動作會更乾淨。目前先只補部位 + 追蹤清單。)
                    for w in (_sh.load_watchlist() or []):
                        if w.get("_error"):
                            continue
                        sym = str(w.get("symbol") or w.get("代號") or "").strip()
                        cur_name = str(w.get("name") or w.get("名稱") or "").strip()
                        if sym and not cur_name:
                            new_name = name_cache.get(sym) or _lookup_stock_name(sym)
                            if new_name:
                                name_cache[sym] = new_name
                                wb = _sw.upsert_watchlist_item(
                                    symbol=sym, 代號=sym,
                                    name=new_name, 名稱=new_name)
                                if wb.get("ok"):
                                    n_filled_trades += 1
                except Exception as e:
                    st.warning(f"⚠️ 補名字失敗(部位/損益還是有重算完):{type(e).__name__}: {e}")

            # 記錄完成時間
            import datetime as _dt
            _tw = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
            done_at = _tw.strftime("%Y-%m-%d %H:%M:%S")
            st.session_state["_last_pos_sync"] = done_at
            msg_parts = [f"✅ 重算完成 {done_at}"]
            if n_filled_pos or n_filled_trades:
                msg_parts.append(
                    f"順便補了 {n_filled_pos} 筆部位名稱、"
                    f"{n_filled_trades} 筆追蹤清單名稱")
            st.success(" / ".join(msg_parts))
            st.toast("📋 部位+損益已更新", icon="✅")

    if st.button("🛑 終止所有跑中", use_container_width=True,
                  help="把所有正在 GitHub 跑的 workflow 全部取消"):
        with st.spinner("⏳ 取消所有跑中的 workflow…"):
            res = _cancel_all_running_workflows()
        if res.get("ok"):
            n = res.get("n_cancelled", 0)
            if n > 0:
                st.success(f"✅ 已取消 {n} 個跑中的 workflow")
            else:
                st.info("沒有跑中的 workflow")
            if res.get("errors"):
                st.warning(f"部分錯誤:{res['errors']}")
        else:
            st.error(f"❌ {res.get('error')}")
        st.rerun()

    if st.button("🔁 重新整理頁面", use_container_width=True,
                  help="重新讀 Sheet 上的最新時間戳跟資料(等於按 F5)"):
        st.rerun()

    st.divider()
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
# 主畫面置中時間戳 — 顯眼放在頂部
# ---------------------------------------------------------------------------
_times = _latest_analysis_times()
_pos_sync_time = st.session_state.get("_last_pos_sync")


def _fmt(t):
    return t or '—'


st.markdown(
    f"""
    <div style="text-align:center; padding: 12px 0 8px 0;
                border-bottom: 1px solid rgba(127,127,127,0.2);
                margin-bottom: 12px;">
        <div style="font-size: 0.85em; color: rgba(127,127,127,0.9);">
            上次更新
        </div>
        <div style="font-size: 0.95em; line-height: 1.7;">
            📋 <b>重算部位+損益</b>:{_fmt(_pos_sync_time)}<br>
            📈 <b>技術-部位</b>:{_fmt(_times['pos_tech'])}<br>
            📈 <b>技術-追蹤</b>:{_fmt(_times['wl_tech'])}<br>
            💎 <b>深度-部位</b>:{_fmt(_times['pos_deep'])}<br>
            💎 <b>深度-追蹤</b>:{_fmt(_times['wl_deep'])}
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


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
