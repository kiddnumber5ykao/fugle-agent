# Fugle Market Data Agent

一個用 [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) 包出來的台股研究子代理,接 [Fugle Market Data API](https://developer.fugle.tw)。專注在**行情監控**與**資料分析 / 回測**,**不做下單**。

> 沒有 API key 也能跑 — 預設會切到 mock 模式,用確定性假資料示範流程。

## 它能做什麼

| 工具 | 用途 |
| --- | --- |
| `get_quote` | 取得即時報價 snapshot(2330、IX0001 加權指數…) |
| `get_candles` | 日 / 週 / 月 K 線歷史(預設過去 180 天) |
| `get_intraday_ticks` | 最近 N 筆盤中逐筆 |
| `get_market_movers` | 上市 / 上櫃漲跌幅排行 |
| `compute_indicators` | 計算 SMA / EMA / RSI |
| `backtest_sma_crossover` | 快慢均線交叉策略回測 |
| `backtest_rsi_mean_reversion` | RSI 均值回歸策略回測 |

回測為 long-only、隔日開盤成交、含可調手續費(預設 5 bps 雙邊),會回報總報酬、買進持有對照、交易次數、勝率與最大回檔。

## 安裝

```bash
cd fugle-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 安裝並登入 Claude CLI(Agent SDK 透過它跑)
# https://docs.claude.com/en/docs/agents-and-tools/claude-agent-sdk
```

## 設定

複製 `.env.example` 為 `.env` 並填入:

```bash
cp .env.example .env
```

| 變數 | 必填 | 說明 |
| --- | --- | --- |
| `FUGLE_MARKETDATA_API_KEY` | 否(沒填會走 mock) | 從 [developer.fugle.tw](https://developer.fugle.tw) 申請 |
| `FUGLE_MOCK` | 否 | 設 `1` 強制走 mock(就算有 key) |
| `FUGLE_RATE_LIMIT_SLEEP` | 否 | REST 呼叫之間 sleep 秒數,預設 0.25 |
| `ANTHROPIC_API_KEY` | 是 | Agent SDK 需要 |

## 試跑

```bash
# Mock 模式 — 不需要任何 key 也能驗證流程
python run.py "看一下 2330 最近 60 天走勢,順便算 20/60 SMA 交叉回測"

# 接真實 Fugle
export FUGLE_MARKETDATA_API_KEY=...your-key...
python run.py "今天台積電報價?跟昨天比怎樣"
```

範例問題:

- 「今天 2330 報價如何?」
- 「拉 0050 過去一年日 K,算 20 日跟 60 日 SMA,告訴我目前是不是黃金交叉」
- 「跑一下 2330 從 2024-01-01 到今天的 RSI 14 均值回歸策略,跟買進持有比較」
- 「台股今天前 5 大漲幅是哪幾檔?」

## 程式呼叫(不走 CLI)

```python
import asyncio
from fugle_agent.agent import run_once

asyncio.run(run_once("幫我看一下 2454 最近走勢,順便用 SMA 20/60 跑回測"))
```

也可以只用工具層(跳過 LLM):

```python
import asyncio
from fugle_agent.tools import get_candles

print(asyncio.run(get_candles({"symbol": "2330"})))
```

## 為什麼這個版本沒有「下單」?

目前需求只到報價/回測,所以工具表刻意 read-only。要加上下單只要:

1. 申請 Fugle Trade(`fugle-trade` Python SDK,另外的金鑰與憑證檔)。
2. 在 `fugle_agent/client.py` 補一個 `TradeClient`。
3. 在 `tools.py` 新增 `place_order` 工具,並在 `agent.py` 把它放進 `allowed_tools`。
4. 強烈建議:加上「需確認」hook,避免 LLM 直接下單。

## 限制與注意事項

- Mock 模式的歷史 K、報價、ticks 都是用 SHA1 種子產生的確定性假資料,**不要拿來做任何真實交易判斷**。
- Fugle 免費方案有 rate limit;真實模式下預設每呼叫間 sleep 0.25 秒,可調。
- 策略回測過去表現不代表未來,不構成任何投資建議。

## 檔案結構

```
fugle-agent/
├── README.md
├── requirements.txt
├── .env.example
├── run.py                       # CLI 進入點
├── fugle_agent/
│   ├── __init__.py
│   ├── config.py                # 環境變數讀取
│   ├── client.py                # Fugle / Mock client façade
│   ├── mock.py                  # 確定性假資料產生器
│   ├── indicators.py            # SMA / EMA / RSI (純 python)
│   ├── backtest.py              # 簡易 long-only 回測引擎
│   ├── tools.py                 # @tool 定義
│   └── agent.py                 # ClaudeSDKClient 主程式
└── tests/
    └── test_smoke.py            # 工具 / 回測 smoke test
```

## 來源

- Fugle Market Data Python SDK — https://github.com/fugle-dev/fugle-marketdata-python
- Claude Agent SDK (Python) — https://github.com/anthropics/claude-agent-sdk-python
- Agent SDK MCP 指南 — https://docs.claude.com/en/api/agent-sdk/mcp
