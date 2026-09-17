# Providers

目前只保留真實 completion 已通過的三家：

| Type | Default model | SSE | Structured output |
|---|---|---:|---|
| `groq` | `openai/gpt-oss-120b` | passed | strict JSON Schema passed |
| `openrouter` | `openrouter/free` | passed | 依動態選中的免費模型而異 |
| `cohere` | `command-a-plus-05-2026` | passed | Cohere JSON object format；僅放 plain-text route |

每家實作位於 `src/beggar_orchestrator/providers/`。新增 Provider 時：

1. 繼承 `LLMProvider`，只設定平台差異。
2. 在 `PROVIDER_TYPES` 註冊 type。
3. 用真實帳戶測 plain SSE、usage、structured output。
4. 通過後才加入 `providers.toml` route。

`openrouter/free` 由 OpenRouter 從目前可用且符合請求能力的免費模型中動態選擇，
不保證每次使用相同模型。需要穩定的 structured output 行為時，應在 route 上指定已驗證的固定模型。
