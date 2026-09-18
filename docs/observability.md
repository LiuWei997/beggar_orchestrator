# Observability

`beggar-orchestrator` 使用 Python 標準 `logging`，不自行新增 handler 或修改 root logger，
因此可以直接併入 FastAPI/Uvicorn 現有的 log 管線。所有事件採固定事件名稱加
`key=value` 欄位，使用同一個 `agent_id` 可追蹤一次請求的完整路徑。

## 建議關注的事件

| Event | 用途 |
|---|---|
| `orchestrator_config_loaded` | 實際讀到的設定檔、provider 與 route 名稱 |
| `orchestrator_provider_configured` | provider type、transport、model 與 credential 是否已設定 |
| `orchestrator_route_configured` | route 允許的 providers 與 deadline |
| `agent_request_configured` | Agent 收到的 route、指定 provider、訊息數、是否要求 structured output |
| `agent_state_changed` | 每一次狀態機轉換與累積耗時 |
| `agent_attempt_started` | 第幾次嘗試、provider、model、單次 timeout 與剩餘 deadline |
| `provider_response_received` | HTTP status、request ID 與收到 response headers 的時間 |
| `provider_http_error` | 上游錯誤碼、限流來源、實際承接 provider、retry-after 與安全錯誤摘要 |
| `provider_cli_process_started` | CLI executable、PID 與工作目錄 |
| `provider_cli_result_received` | CLI result status、conversation/request ID 與耗時 |
| `provider_request_completed` | transport、provider、model、request ID、token、延遲與輸出長度 |
| `agent_attempt_failed` | retryable、HTTP status、錯誤分類與上游診斷欄位 |
| `agent_provider_not_allowed` | 呼叫端指定的 provider 不在該 route |
| `agent_provider_missing` | 指定 provider 未設定或已停用 |
| `agent_run_failed` | 指定 provider 的最終失敗狀態與錯誤分類 |

## 常見排查順序

1. 用 `agent_id` 篩出單次請求。
2. 從 `agent_attempt_started` 確認 route 實際選到的 provider 與 model。
3. HTTP provider 看 `provider_response_received`、`provider_http_error`；CLI provider 看
   `provider_cli_process_started`、`provider_cli_result_received`。
4. 看 `agent_attempt_failed` 的 `status_code`、
   `provider_code`、`limit_source`、`upstream_provider`、`retry_after` 與 `remedy_hint`
   用來區分認證、額度、模型、上游與暫時性問題。
5. 最後以 `agent_state_changed` 和 `agent_run_failed` 確認在哪個狀態結束。

## 安全邊界

Log 不包含 prompt、system instruction、字幕、完整模型回應、request body、JSON Schema、
Authorization header 或 API key。Provider 回傳的錯誤摘要會限制長度，並遮蔽常見的
Bearer token、`sk-`、Google API key 與命名為 API key/access token/authorization 的值。
若要檢查模型輸出格式，應由呼叫端在受控儲存區另行保存，不應提高 orchestrator log
內容的敏感度。
