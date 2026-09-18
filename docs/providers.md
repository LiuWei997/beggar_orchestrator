# Providers

目前只保留真實 completion 已通過的 Provider：

| Type | Default model | SSE | Structured output |
|---|---|---:|---|
| `groq` | `openai/gpt-oss-120b` | passed | strict JSON Schema passed |
| `openrouter` | `openrouter/free` | passed | 依動態選中的免費模型而異 |
| `cohere` | `command-a-plus-05-2026` | passed | Cohere JSON object format；僅放 plain-text route |
| `agy` | `gemini-3.8-flash-low` | NDJSON passed | 原生 `--json-schema` passed |

每家實作位於 `src/beggar_orchestrator/providers/`。新增 Provider 時：

1. HTTP API 繼承 `LLMProvider`；本機工具繼承 `CLIProvider`；兩者都遵守 `Provider` protocol。
2. 在 `PROVIDER_TYPES` 註冊 type。
3. 用真實帳戶測 plain SSE、usage、structured output。
4. 通過後才加入 `providers.toml` route。

`openrouter/free` 由 OpenRouter 從目前可用且符合請求能力的免費模型中動態選擇，
不保證每次使用相同模型。需要穩定的 structured output 行為時，應在 route 上指定已驗證的固定模型。

## Antigravity CLI

`agy` 使用本機已登入的 cached credential，不需要在 `providers.toml` 設定 credential：

```toml
[providers.agy]
type = "agy"
model = "gemini-3.8-flash-low"
executable = "/Users/you/.local/bin/agy"
```

`agy` 1.2.6 與官方 headless 文件都沒有 `--system` flag。Adapter 會把 Agent 的 system
instruction 放入明確標記的 prompt 區段；若傳入 `response_schema`，則使用原生
`--json-schema` 強制結構。Prompt 透過 stdin 的 stream-json 傳入，不放在 process argv。
