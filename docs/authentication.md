# Authentication

本機開發使用作業系統 Keychain：

```bash
beggar-auth set groq
beggar-auth status groq
beggar-auth verify groq --config providers.toml
beggar-auth delete groq
```

正式環境可以使用環境變數：

```toml
[providers.groq]
type = "groq"
credential = "env:GROQ_API_KEY"
enabled = true
```

`verify` 會送出一個真實、低 token 的 SSE completion，因此同時驗證 token、model 與
可用 quota，而不只是呼叫 `/models`。

不得把 Key 寫入 TOML、Git、SQLite、log 或聊天訊息。帳號註冊、CAPTCHA、手機、實名、
條款與付款仍由使用者處理。
