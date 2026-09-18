# Architecture

Runtime 對外仍然只有三個概念：設定、一次性 Agent、Provider 實作。程式碼內部
再依單一職責拆開，避免公開 `Agent` 同時處理狀態機、訊息驗證和 Provider 重試：

```text
agent.py          公開 Agent API 與 async context 資源管理
execution.py      單一指定 Provider 的 SSE、timeout、cancel 與結果保留
lifecycle.py      狀態、snapshot、transition 與狀態機規則
messages.py       Message 型別、role 驗證與 system message 注入
runtime.py        Provider/Route 集合與 lifecycle callback
config.py         TOML cache、token 讀取、Provider/Route 建置、認證 CLI
providers/        API 與本機 CLI 的 adapter 實作
```

依賴方向是 `agent.py -> execution.py -> runtime.py/providers/`，狀態與訊息物件作為小型的
共用模組。`config.py` 由 `Agent` 建置時延遲匯入，避免 configuration 與公開 API
相互匯入。

## Configuration

公開的 `Agent` 依約定自動讀取 `providers.toml`；也能以 `BEGGAR_CONFIG` 或
`config_path` 指定位置。內部 configuration loader 解析 Keychain 或環境變數 token，
建立 Provider 及 Route。`beggar-auth` CLI 也由這個模組提供。

## Agent

公開呼叫端只有一次性的 `Agent`。它保存固定 system instruction；`start()` 回傳完整
`Response`，`stream()` 即時產生 `connected`、`content_delta`，最後產生包含完整內容
與 usage 的 `completed`。同一個 Agent 不可啟動第二次。

推薦以 `async with agent` 管理 SSE 資源，再用 `async for event in agent.stream(...)`
消費事件。正常讀完保留 `RUN_COMPLETED`；提早 break 或區塊拋錯時，context manager
會關閉 active iterator、取消未完成 Agent，並保留 partial output 供離開區塊後讀取。

呼叫端必須明確指定 Provider。Route 只確認該 Provider 是否允許並提供 model/timeout；
Provider 不存在、額度不足或呼叫失敗時直接終止，不會 fallback。

Agent 可透過 `status` 或 `snapshot()` 查詢即時生命週期，也可取消；終止後資料仍可讀，
直到呼叫 `await agent.clear()` 釋放。`on_event` callback 會收到同一組生命週期變化，供
外部 server log 或 OpenTelemetry 使用。

生命週期由有限狀態機維護，每次合法轉換都保存在 `snapshot().transitions`；非法跳轉會
拋出 `InvalidAgentTransitionError`。主要路徑如下：

```text
RUN_CREATED -> INIT_FAILED
RUN_CREATED -> ATTEMPT_STARTED -> CONNECTED -> RUN_COMPLETED

RUN_CREATED / ATTEMPT_STARTED / CONNECTED -> RUN_CANCELLED
RUN_CREATED / ATTEMPT_STARTED / CONNECTED -> RUN_TIMED_OUT
RUN_CREATED / ATTEMPT_STARTED / CONNECTED -> OUT_OF_USAGE
```

`INIT_FAILED`、`OUT_OF_USAGE`、`RUN_COMPLETED`、`RUN_TIMED_OUT`、
`RUN_CANCELLED` 都是終止狀態，不允許再轉換。`clear()` 是資源釋放操作，不是狀態。

## Providers

Runtime 只依賴公開的 `Provider` protocol，因此 route 與 `AgentExecution` 不需要知道
後端是 HTTP API 或本機 subprocess。`LLMProvider` 實作共用 OpenAI-compatible SSE；
`CLIProvider` 負責 subprocess 啟停與取消；`AgyProvider` 再處理 Antigravity 的 NDJSON、
cached login、JSON Schema 與 usage 格式。未來新增 CLI 時只需實作同一個 `Provider`
contract，不必修改 Agent 執行或狀態機。

套件不負責 Web server、Prompt 資料庫、conversation persistence 或自動執行 tools。
外部 workflow 擁有完整訊息歷史，`tool` role 目前代表已執行工具的結果。

## Observability

設定載入、明確 Provider 選擇、HTTP/CLI transport、狀態機轉換
與最終結果都有固定名稱的結構化 log。`agent_id` 是跨層關聯鍵；Provider request ID 或
CLI conversation ID 則用於對照上游。欄位與安全限制見 [Observability](observability.md)。
