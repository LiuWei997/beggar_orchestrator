# beggar-orchestrator

一個只負責 LLM API 呼叫、路由、SSE 與生命週期的 Python 套件。目前已實測 Groq、
OpenRouter、Cohere。

公開呼叫端只有一個 `Agent`。每個 Agent 只能執行一次；下一次對話請建立新的 Agent。
內部模組已按單一職責分為公開介面、單次執行、生命週期、訊息處理、路由執行環境與
Provider 實作；細節見 [Architecture](docs/architecture.md)。

## 安裝

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[auth,dev]'
cp examples/providers.toml providers.toml
```

把 API Key 存進作業系統 Keychain：

```bash
beggar-auth set groq
beggar-auth status groq
beggar-auth verify groq --config providers.toml
```

## Provider 自動配置約定

`Agent` 初始化時自動載入目前工作目錄的 `providers.toml`，並根據其中的 credential
名稱讀取環境變數或 Keychain。使用者不需要建立 Provider 物件。

同一個設定檔依絕對路徑只會開啟並解析一次；解析成功的設定會保存在目前 Python process
的記憶體中。之後建立 Agent 不會再次對該 TOML 進行硬碟 I/O。重新啟動 process 才會
重新讀取設定檔。

設定檔位置依下列順序決定：

1. `Agent(config_path=...)`
2. 環境變數 `BEGGAR_CONFIG`
3. 目前工作目錄的 `providers.toml`

如果設定檔不存在、格式錯誤或沒有可用 Provider，Agent 會建立成功但狀態直接成為
`INIT_FAILED`，可透過 `agent.snapshot().errors` 查看原因。

## 核心物件與職責

整體協作關係如下：

```text
providers.toml -> Settings -> ProviderRuntime
                                  |
Message -> Agent -> AgentExecution -> LLMProvider -> StreamEvent -> Response
                |                       |
                +-> AgentStateMachine <-+
                         |
                    AgentSnapshot
```

### 公開物件

這些是外部系統可以直接匯入與使用的物件。

| 物件 | 負責功能 | 不負責 | 原始碼 |
| --- | --- | --- | --- |
| `Agent` | 對外的單次對話介面；固定 system instruction，自動建立設定與 Provider，提供 `start()`、`stream()`、`cancel()`、`clear()` 與 async context manager | 不保存多輪對話、不執行 tool、不調度多個 Agent | [`agent.py`](src/beggar_orchestrator/agent.py#L15) |
| `Message` | 表示一則 `developer`、`user`、`assistant` 或 `tool` 訊息；提供 `Message.user()` 等建立方法。System instruction 則由 `Agent(system=...)` 統一管理 | 不保存 conversation session，不執行 tool call | [`messages.py`](src/beggar_orchestrator/messages.py#L11) |
| `StreamEvent` | SSE 消費者每次收到的統一事件；攜帶 event type、Provider、model、delta 或最終 response | 不累積完整內容；累積由 `AgentExecution` 處理 | [`providers/base.py`](src/beggar_orchestrator/providers/base.py#L55) |
| `StreamEventType` | 區分 `CONNECTED`、`CONTENT_DELTA`、`COMPLETED` 三種串流事件 | 不代表 Agent 生命週期 | [`providers/base.py`](src/beggar_orchestrator/providers/base.py#L48) |
| `Response` | 一次成功呼叫的最終不可變結果；包含內容、Provider、model、request ID、latency、token usage 與 finish reason | 不包含失敗歷史或生命週期 | [`providers/base.py`](src/beggar_orchestrator/providers/base.py#L37) |
| `Usage` | 統一表示 input、output 與 total token 數量 | 不計算費用、不查詢剩餘額度 | [`providers/base.py`](src/beggar_orchestrator/providers/base.py#L30) |
| `AgentStatus` | Agent 生命週期的枚舉，例如 `RUN_CREATED`、`CONNECTED` 和 `RUN_COMPLETED` | 不自己執行轉移檢查 | [`lifecycle.py`](src/beggar_orchestrator/lifecycle.py#L34) |
| `AgentSnapshot` | 某個時點的完整可讀狀態；包含當前狀態、選中 Provider/model、partial output、錯誤、取消原因與轉移歷史 | 不能用來修改 Agent | [`lifecycle.py`](src/beggar_orchestrator/lifecycle.py#L63) |
| `AgentTransition` | 一筆已發生的狀態轉移；記錄 previous、current 與 timestamp | 不決定轉移是否合法 | [`lifecycle.py`](src/beggar_orchestrator/lifecycle.py#L56) |

### 內部協作物件

這些物件是套件的內部實作細節。外部程式應使用 `Agent`，不應自行建立或依賴它們。

| 物件 | 單一職責 | 與其他物件的關係 | 原始碼 |
| --- | --- | --- | --- |
| `AgentExecution` | 執行一次不可重用的請求；選擇 Route target、控制 timeout/fallback/cancel、消費 Provider SSE、累積 partial output 並保留結果 | 由 `Agent` 擁有，透過 `ProviderRuntime` 找 Provider，並將所有狀態改變送給 `AgentStateMachine` | [`execution.py`](src/beggar_orchestrator/execution.py#L24) |
| `AgentStateMachine` | 集中定義合法狀態轉移，拒絕非法轉移，並記錄完整歷史 | 只管狀態規則；不呼叫 API、不處理 SSE | [`lifecycle.py`](src/beggar_orchestrator/lifecycle.py#L83) |
| `ProviderRuntime` | 保有這次 Agent 可用的 Providers、Routes、system instruction、circuit breaker 與 lifecycle callback | 由 configuration builder 建立，供 `AgentExecution` 查詢 | [`runtime.py`](src/beggar_orchestrator/runtime.py#L57) |
| `Route` | 定義一組有順序的 Provider targets、最多嘗試次數與整體 deadline | 被 `AgentExecution` 用來決定下一個嘗試對象 | [`runtime.py`](src/beggar_orchestrator/runtime.py#L25) |
| `RouteTarget` | 定義 Route 中一個候選 Provider、model override、priority 與單次 timeout | 不包含 API key，不自己發出請求 | [`runtime.py`](src/beggar_orchestrator/runtime.py#L16) |
| `ProviderCircuitBreaker` | 計算 Provider 的連續可重試失敗；達門檻後在 cooldown 期間暫時跳過該 Provider | 由 `ProviderRuntime` 持有，成功時清除失敗計數 | [`runtime.py`](src/beggar_orchestrator/runtime.py#L31) |
| `Settings` | 保存 TOML 解析後的 Provider 與 Route 原始設定，並作為 process 內 cache 的值 | 不含已建立的 Provider client，不發出網路請求 | [`config.py`](src/beggar_orchestrator/config.py#L21) |
| `LLMProvider` | 將統一 messages 轉為 OpenAI-compatible HTTP request、解析 SSE、正規化錯誤、產生 `StreamEvent` 與 `Response` | 不決定 fallback、不管 Agent 生命週期 | [`providers/base.py`](src/beggar_orchestrator/providers/base.py#L64) |
| `GroqProvider` / `OpenRouterProvider` / `CohereProvider` | 只定義平台特有的 endpoint、預設模型、headers 或 structured-output 格式 | 共用 HTTP/SSE 邏輯由 `LLMProvider` 處理 | [`providers/`](src/beggar_orchestrator/providers) |

### 錯誤物件

| 錯誤 | 使用時機 |
| --- | --- |
| `AgentError` | Agent 層的基礎錯誤，例如未知 Route 或沒有有效訊息 |
| `AgentAlreadyStartedError` | 同一個單次 Agent 被再次啟動、設定或進入 context |
| `AgentClearedError` | `clear()` 之後繼續讀取或操作 Agent |
| `InvalidAgentTransitionError` | 請求不在 `_allowed` 表中的生命週期轉移 |
| `AllProvidersFailed` | Route 內所有可嘗試 Provider 都未完成請求；`errors` 保留各次失敗 |
| `ProviderError` | Provider 層的基礎錯誤；`retryable` 告訴路由層是否應記錄為可重試失敗 |
| `AuthenticationError` | API key 遺失、無效或被 Provider 拒絕；不可重試 |
| `RateLimitError` | Provider 回傳 HTTP 429；可重試，可觸發 circuit breaker |
| `ConfigError` | TOML、credential reference、Keychain dependency 或 Provider type 設定錯誤 |

## 啟動對話

```python
import asyncio

from beggar_orchestrator import Agent, Message


async def main():
    agent = Agent(system="You are a senior DevOps engineer.")

    response = await agent.start(
        route="general-chat",
        messages=[Message.user("用一句話說明目前服務狀態")],
        max_output_tokens=500,
    )
    print(response.content)


asyncio.run(main())
```

`system` 在 Agent 初始化時固定。`user`、`assistant`、`developer`、`tool` messages 在
`start()` 或 `stream()` 時傳入。`start()` 內部仍使用 SSE，但只回傳完整 Response。

## SSE 串流與自動關閉

```python
import asyncio

from beggar_orchestrator import Agent, Message, StreamEventType


async def main():
    agent = Agent(system="You are a senior DevOps engineer.")

    async with agent:
        async for event in agent.stream(
            route="general-chat",
            messages=[Message.user("分析 production logs")],
        ):
            if event.type is StreamEventType.CONTENT_DELTA:
                print(event.content, end="", flush=True)
            elif event.type is StreamEventType.COMPLETED:
                final_response = event.response

    print(agent.status)
    print(final_response.usage)


asyncio.run(main())
```

正常讀完後狀態是 `RUN_COMPLETED`。如果中途 `break`、區塊拋出例外或提早離開，
`async with` 會關閉 Provider stream 並將狀態改成 `RUN_CANCELLED`。

## 強制停止輸出中的 Agent

在 context 中直接中斷：

```python
async with agent:
    async for event in agent.stream(
        route="general-chat",
        messages=[Message.user("產生一篇長文章")],
    ):
        if event.type is StreamEventType.CONTENT_DELTA:
            print(event.content, end="", flush=True)
            break

snapshot = agent.snapshot()
print(snapshot.status)   # RUN_CANCELLED
print(snapshot.content)  # 已經輸出的部分內容
```

由外部 Supervisor 或斷線監視器停止：

```python
stream_task = asyncio.create_task(consume(agent))
await agent.cancel("Client disconnected")
await stream_task
```

`cancel()` 是不可恢復的終止，不是可 resume 的 pause。

## 生命週期

透過 `agent.status` 查目前狀態，或用 `agent.snapshot()` 取得 Provider、模型、嘗試次數、
partial output、錯誤和完整轉換歷史：

```python
snapshot = agent.snapshot()

print(agent.status)          # 目前狀態
print(agent.is_terminal)     # 是否已進入終止狀態
print(snapshot.provider)     # 目前或最後使用的 Provider
print(snapshot.attempt)      # 已嘗試次數
print(snapshot.content)      # 目前累積的輸出
print(snapshot.errors)       # 失敗過的 Provider 與錯誤

for transition in snapshot.transitions:
    print(transition.previous, "->", transition.current, transition.occurred_at)
```

| 狀態 | 含義 | 終止 | 可轉換到 | 實際觸發位置 |
| --- | --- | --- | --- | --- |
| `RUN_CREATED` | Agent 剛建立，尚未開始呼叫 Provider | 否 | `INIT_FAILED`、`ATTEMPT_STARTED`、`OUT_OF_USAGE`、`RUN_TIMED_OUT`、`RUN_CANCELLED` | [`AgentStateMachine.__init__`](src/beggar_orchestrator/lifecycle.py#L121) |
| `INIT_FAILED` | 設定載入失敗，或沒有任何可用 Provider | 是 | 無 | [`AgentExecution.__init__`](src/beggar_orchestrator/execution.py#L55) |
| `ATTEMPT_STARTED` | 開始嘗試某個 Provider | 否 | `ATTEMPT_STARTED`、`CONNECTED`、`OUT_OF_USAGE`、`RUN_TIMED_OUT`、`RUN_CANCELLED` | [開始 Provider attempt](src/beggar_orchestrator/execution.py#L169) |
| `CONNECTED` | 已連接 Provider，可能正在接收 SSE 內容 | 否 | `ATTEMPT_STARTED`、`OUT_OF_USAGE`、`RUN_COMPLETED`、`RUN_TIMED_OUT`、`RUN_CANCELLED` | [收到 `CONNECTED` event](src/beggar_orchestrator/execution.py#L192) |
| `OUT_OF_USAGE` | Route 允許的 Provider 已全部嘗試失敗 | 是 | 無 | [未知 Route](src/beggar_orchestrator/execution.py#L145)、[已輸出後失敗](src/beggar_orchestrator/execution.py#L245)、[所有嘗試失敗](src/beggar_orchestrator/execution.py#L254) |
| `RUN_COMPLETED` | 對話正常完成，結果可透過 `response` 或 `snapshot()` 讀取 | 是 | 無 | [Provider 完整回應](src/beggar_orchestrator/execution.py#L228) |
| `RUN_TIMED_OUT` | 單次 Provider timeout 或整體 deadline 已到 | 是 | 無 | [timeout 結算](src/beggar_orchestrator/execution.py#L252) |
| `RUN_CANCELLED` | 被外部取消、中途離開 context，或串流未讀完 | 是 | 無 | [取消尚未啟動的 Agent](src/beggar_orchestrator/execution.py#L269)、[取消執行中 Agent](src/beggar_orchestrator/execution.py#L370) |

### 狀態機如何檢查轉移

所有執行邏輯都必須透過
[`AgentExecution.transition_to()`](src/beggar_orchestrator/execution.py#L313) 申請轉移。該方法會將目標狀態交給
[`AgentStateMachine.transition()`](src/beggar_orchestrator/lifecycle.py#L137) 檢查：

1. 以目前狀態查詢 [`_allowed`](src/beggar_orchestrator/lifecycle.py#L86) 轉移表。
2. 目標狀態不在允許清單時，拋出 `InvalidAgentTransitionError`，且不改變狀態。
3. 合法時更新目前狀態，並將 `previous`、`current`、`occurred_at` 寫入歷史。
4. `AgentExecution.transition_to()` 接著更新 Provider、model、attempt 與時間，再通知 `on_event` callback。

`CONNECTED -> ATTEMPT_STARTED` 只會發生在尚未輸出任何內容、仍允許 fallback
到下一個 Provider 時。一旦開始輸出內容，就不會切換 Provider，避免混合不同模型的回答。

同一個 Agent 第二次呼叫 `start()` 或 `stream()` 會拋出
`AgentAlreadyStartedError`。終止後結果仍可讀；確認不再需要資料時呼叫：

```python
await agent.clear()
```

清除後 `agent.is_cleared` 為 `True`，再次讀取 status、response 或 snapshot 會拋出
`AgentClearedError`。

## 路由與 fallback

Route 依 `priority` 嘗試 Provider，並受 `max_attempts`、各 Provider timeout 與整體
deadline 限制。只要尚未輸出內容，失敗便會嘗試下一家；開始輸出後不會 fallback，
避免把兩個模型的回答混在一起。

`OUT_OF_USAGE` 表示 Route 允許的 Provider 嘗試已耗盡，不保證錯誤原因一定是 API
額度不足。API Key 不得放入 TOML、Git、server log 或對話內容。
