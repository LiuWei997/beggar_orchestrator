# Providers

目前只保留真實 completion 已通過的三家：

| Type | Default model | SSE | Structured output |
|---|---|---:|---|
| `groq` | `openai/gpt-oss-120b` | passed | strict JSON Schema passed |
| `openrouter` | `dots-studio/dots-3-note-preview:free` | passed | strict JSON Schema passed |
| `cohere` | `command-a-plus-05-2026` | passed | Cohere JSON object format；僅放 plain-text route |

每家實作位於 `src/beggar_orchestrator/providers/`。新增 Provider 時：

1. 繼承 `LLMProvider`，只設定平台差異。
2. 在 `PROVIDER_TYPES` 註冊 type。
3. 用真實帳戶測 plain SSE、usage、structured output。
4. 通過後才加入 `providers.toml` route。

OpenRouter Dots3 預計於 2026-09-30 下線，需要在到期前替換。
