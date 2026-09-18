# LLM API integration status

Last reviewed: 2026-09-18

This is the source of truth for upstream provider onboarding. A built-in preset is not considered usable until authentication and a real model request have both passed with the intended account.

## Status meanings

- `implementation-ready`: Provider class, credential reference and known protocol differences exist in code.
- `auth-ready`: A credential is stored outside the repository and authentication verification passed.
- `smoke-passed`: A minimal real model request completed and returned parseable usage data.
- `route-ready`: Required structured-output behavior passed and the provider may be explicitly selected for a route.
- `plain-only`: Authentication and plain completion passed, but structured output is not reliable.
- `blocked`: A concrete account, region, permission or protocol issue prevents progress.

## Provider matrix

| Order | Provider | Type | Default model | Authentication | Auth | Smoke | Route | Notes |
|---:|---|---|---|---|---|---|---|---|
| 1 | Groq | `groq` | `openai/gpt-oss-120b` | Bearer API key | passed | passed | passed | Plain and strict JSON Schema requests passed; reasoning models need at least a 128-token output budget for short answers |
| 2 | OpenRouter | `openrouter` | `openrouter/free` | Bearer API key | passed | passed | model-dependent | Dynamic free-model router; the selected model and structured-output behavior may vary between requests |
| 3 | Cohere | `cohere` | `command-a-plus-05-2026` | Bearer API key | passed | passed | plain-only | Free Trial key; plain completion works, but the current compatibility response was not directly parseable as required JSON |
| 4 | Antigravity CLI | `agy` | `gemini-3.8-flash-low` | Local cached login | passed | passed | passed | Local NDJSON subprocess; native `--json-schema`; system instruction is encoded into the prompt because agy 1.2.6 has no `--system` flag |

Groq remains the stable structured-output target. OpenRouter uses its dynamic free-model router,
so structured-output support depends on the model selected for that request. Cohere is enabled only
for explicit selection in the local `general-chat` plain-text route. Provider implementations that have not
completed a real model request are not retained.

## Removed providers

| Provider | Last result | Removal reason |
|---|---|---|
| Cerebras | Authentication passed; completion returned HTTP 402 `payment_required` | Tested, but this account had no usable free API quota |
| Mistral | Model listing passed; repeated completions returned HTTP 429 `rate_limited` | Tested, but no usable free completion quota was available to this account |
| SiliconFlow Global | Model listing passed; both `zai-org/GLM-5.3-Flash` and `tencent/Hy4-preview` returned HTTP 402 | Tested, but this account had no usable free API credit |
| Zhipu GLM | Not tested with a credential | Removed because it never reached the acceptance threshold |
| Cloudflare Workers AI | Not tested with a credential and Account ID | Removed because it never reached the acceptance threshold |
| Tencent TokenHub | Not tested with a credential | Removed because it never reached the acceptance threshold |
| Alibaba Model Studio Singapore | Not tested with a credential and Workspace ID | Removed because it never reached the acceptance threshold |
| NVIDIA hosted NIM | Authentication and `/v1/models` passed; `moonshotai/kimi-k3` timed out at 30, 60 and 120 seconds, while `meta/llama-3.3-70b-instruct` was deprecated | Tested, but no usable hosted completion was available; implementation, configuration and local credentials were removed |
| ModelScope International | `/v1/models` returned 34 models, but completion returned HTTP 401 requiring an Alibaba Cloud account binding | Tested, but the account was not inference-ready; no implementation was added and the local credential was removed |

Credentials for removed providers were removed from the local plaintext store. No credential
existed in the operating-system Keychain for ModelScope at cleanup time.

## New free-tier candidates

These are research candidates, not built-in implementations. Add one only after a credential and a real
completion both pass.

| Priority | Provider | Confirmed free API allowance | Compatibility | Suggested first model |
|---:|---|---|---|---|
| 1 | Google Gemini Developer API | Selected models have free input and output tokens; model-specific rate limits apply | Official OpenAI-compatible Chat Completions endpoint | `gemini-3.8-flash` |
| 2 | Vercel AI Gateway | US$5 monthly credit for accounts that have never purchased credits; starts on first request | OpenAI Chat Completions, Responses and Anthropic Messages compatible | `bytedance/seed-2.1-turbo` for strict structured output; `inclusionai/ling-3.0-flash-vl-free` for zero token price |
| 3 | SambaNova Cloud | No-card Free Tier: 20 requests/day and 200,000 tokens/day on documented production and preview models | OpenAI-compatible `https://api.sambanova.ai/v1` | `gpt-oss-120b` |
| 4 | Hugging Face Inference Providers | Free users receive US$0.10 monthly routed-inference credit | OpenAI-compatible `https://router.huggingface.co/v1` | Select a live low-cost chat model from `/v1/models` |

GitHub Models is deliberately excluded: GitHub retired its playground, catalog and inference API
on 2026-07-30. NVIDIA labels the hosted `moonshotai/kimi-k3` prototype as a Free Endpoint, but
three authenticated completion attempts produced no response headers before their deadlines;
the provider was removed after failing the acceptance test.

## Per-provider acceptance test

Complete these checks in order and record the outcome below:

1. Copy `examples/providers.toml` to a local untracked `providers.toml`.
2. Enable one provider and select its `type`, credential reference and optional model override.
3. Add the provider to an enabled route.
4. Run `beggar-auth set <provider>` and enter the key through hidden terminal input.
5. Run `beggar-auth verify <provider> --config providers.toml`.
6. Send a short plain-text request with enough output budget for reasoning overhead; use at least 128 tokens for GPT-OSS.
7. Send a small JSON-schema request using the workflow's real response shape.
8. Confirm provider, model, request ID, token usage and latency appear in the final response/event.
9. Trigger one controlled failure and confirm the Agent stops without calling another provider.
10. Change the provider status to `route-ready` only after all required checks pass.

Authentication failures do not fall back to another provider because they indicate configuration problems. Never record a raw API key, browser cookie or authorization header in this file.

## Test record

Add one row for every live attempt, including failures. Do not overwrite earlier evidence.

| Time (Asia/Taipei) | Provider | Model | Test | Result | Latency | Usage | Request ID / safe error |
|---|---|---|---|---|---:|---|---|
| 2026-09-14 23:24 | Groq | `openai/gpt-oss-120b` | Minimal completion, 8-token cap | passed; response body was empty because 6 of 8 completion tokens were reasoning tokens | 31 ms upstream | 75 in / 8 out / 83 total | request ID recorded locally during test |
| 2026-09-14 23:46 | Cohere | `command-a-plus-05-2026` | OpenAI-compatible minimal completion | passed; returned `OK` | not recorded | 4 in / 54 out / 58 total (53 reasoning) | `b0d5879a-9dab-4f69-9e75-af4e0a3a89f9` |
| 2026-09-15 00:00 | Cerebras | `gpt-oss-120b` | OpenAI-compatible minimal completion | blocked; HTTP 402 after successful key authentication | under 1 s | none | `payment_required`: quota requires billing |
| 2026-09-15 07:29 | Groq | `openai/gpt-oss-120b` | Minimal completion, 64-token cap | passed; returned `OK` | under 1 s | 75 in / 47 out / 122 total | `chatcmpl-562d03d2-4489-4d98-9741-dfa4cb770276` |
| 2026-09-15 07:29 | OpenRouter | `openrouter/free` | Minimal completion | passed through `cohere/north-mini-code:free`; cost reported as zero | under 1 s | 4 in / 35 out / 39 total | `gen-1789428550-w3HTGccYPT2wCxPBuUYz` |
| 2026-09-15 07:29 | Cohere | `command-a-plus-05-2026` | Minimal completion | passed; returned content and usage, but did not obey exact-output wording on this attempt | under 1 s | 4 in / 62 out / 66 total | `2a474620-8fb7-4cec-95bd-7fd4a7f04d95` |
| 2026-09-15 07:29 | Mistral | `mistral-small-2603` | Minimal completion plus delayed retry | blocked; both completions returned HTTP 429, while `/models` returned 46 models | under 1 s each | none | `rate_limited` / code `1300` |
| 2026-09-15 07:29 | Cerebras | `gpt-oss-120b` | Minimal completion retry | blocked; HTTP 402 | under 1 s | none | `payment_required` |
| 2026-09-15 07:31 | Groq | `openai/gpt-oss-120b` | Strict JSON Schema | passed and parsed as the exact required object | 518 ms | 241 total | request ID present |
| 2026-09-15 07:31 | OpenRouter | `openrouter/free` | Strict JSON Schema | plain completion succeeded, but returned content was not directly parseable as the required JSON object | 1.7 s | 53 total in preceding package probe | request ID present |
| 2026-09-15 07:31 | Cohere | `command-a-plus-05-2026` | Strict JSON Schema | plain completion succeeded, but returned content was not directly parseable as the required JSON object | 799 ms in preceding package probe | 133 total in preceding package probe | request ID present |
| 2026-09-15 10:08 | OpenRouter | `cohere/north-mini-code:free` | Fixed-model plain completion | passed; returned exactly `NORTH_OK` | 1,144 ms | 54 total | request ID present |
| 2026-09-15 10:08 | OpenRouter | `cohere/north-mini-code:free` | JSON Schema probe | HTTP passed, but returned content was not directly parseable as the required object | 1,959 ms | 161 total | request ID present |
| 2026-09-15 10:10 | OpenRouter | `dots-studio/dots-3-note-preview:free` | Fixed-model plain completion | passed; returned exactly `DOTS_OK` | 2,193 ms | 92 total | request ID present |
| 2026-09-15 10:10 | OpenRouter | `dots-studio/dots-3-note-preview:free` | Strict JSON Schema | passed and parsed as the exact required object | 2,274 ms | 112 total | request ID present |
| 2026-09-15 | SiliconFlow Global | `zai-org/GLM-5.3-Flash` | Plain and JSON Schema completion | blocked; both returned HTTP 402 | 3.1 s / 4.5 s | none | code `30001`: insufficient balance |
| 2026-09-15 | SiliconFlow Global | `tencent/Hy4-preview` | Minimal completion | blocked; model exists but completion returned HTTP 402 | 755 ms | none | code `30001`: insufficient balance |
| 2026-09-15 | Vercel AI Gateway | `bytedance/seed-2.1-turbo` | Strict JSON Schema completion | blocked before inference; supplied credential was a valid Vercel account token but not an AI Gateway key | 614 ms | none | HTTP 401 `authentication_error` |
| 2026-09-15 | Groq | `openai/gpt-oss-120b` | SSE completion | passed; content delta and final usage received | under 2 s concurrent probe | 152 total | finish reason `stop` |
| 2026-09-15 | OpenRouter | `dots-studio/dots-3-note-preview:free` | SSE completion | passed; content delta and final usage received | under 2 s concurrent probe | 46 total | finish reason `stop` |
| 2026-09-15 | Cohere | `command-a-plus-05-2026` | SSE completion with usage option | passed; content delta and final usage received | under 2 s concurrent probe | 89 total | compatibility endpoint |
| 2026-09-16 | NVIDIA hosted NIM | `moonshotai/kimi-k3` | Minimal SSE completion | blocked; no response headers before the 30-second read timeout | 30 s | none | no request ID received |
| 2026-09-16 | NVIDIA hosted NIM | `moonshotai/kimi-k3` | Minimal SSE retry | blocked; no response headers before the 60-second read timeout | 60 s | none | no request ID received |
| 2026-09-16 | NVIDIA hosted NIM | `/v1/models` with replacement credential | Authentication and model discovery | passed; 81 model IDs returned and Kimi K3 was listed | under 1 s | none | HTTP 200 |
| 2026-09-16 | NVIDIA hosted NIM | `moonshotai/kimi-k3` | Minimal SSE with replacement credential and prototype headers | blocked; no response headers before the 120-second read timeout | 120 s | none | no request ID received |
| 2026-09-16 | NVIDIA hosted NIM | `meta/llama-3.3-70b-instruct` | Availability check | unavailable on hosted endpoint; absent from `/v1/models` and officially deprecated | under 1 s | none | use partner endpoint or self-hosted NIM |
| 2026-09-16 | ModelScope International | `Qwen/Qwen3.5-35B-A3B` | Minimal completion | blocked; HTTP 401 requires binding an Alibaba Cloud account | under 1 s | none | credential removed after test |
| 2026-09-16 | OpenRouter | `openrouter/free` | Package SSE verification after default change | passed through `nvidia/nemotron-3-ultra-550b-a55b:free` | 1,324 ms | 61 total | request ID present |
| 2026-09-18 18:47 | Antigravity CLI | `gemini-3.8-flash-low` | Local NDJSON plus strict JSON Schema | passed; parsed exact `{\"ok\":true,\"value\":2}` response | 6,158 ms | 13,450 in / 33 out / 13,483 total | local conversation ID received |

## Provider references

- Groq models: <https://console.groq.com/docs/models>
- Cerebras model selection: <https://inference-docs.cerebras.ai/models/choose-a-model>
- OpenRouter free model router: <https://openrouter.ai/docs/cookbook/get-started/free-models-router-playground>
- Mistral models: <https://docs.mistral.ai/models>
- Cloudflare Workers AI models: <https://developers.cloudflare.com/workers-ai/models/>
- Cohere models: <https://docs.cohere.com/docs/models>
- Zhipu GLM-4.7-Flash: <https://docs.bigmodel.cn/cn/guide/models/free/glm-4.7-flash>
- SiliconFlow: <https://docs.siliconflow.cn/docs/api/chat-completions-post>
- Tencent TokenHub models: <https://cloud.tencent.com/document/product/1823/132358>
- Alibaba Model Studio models: <https://www.alibabacloud.com/help/en/model-studio/models>
- Google Gemini pricing: <https://ai.google.dev/gemini-api/docs/pricing>
- Google Gemini OpenAI compatibility: <https://ai.google.dev/gemini-api/docs/openai>
- Vercel AI Gateway pricing: <https://vercel.com/docs/ai-gateway/pricing>
- SambaNova free-tier limits: <https://docs.sambanova.ai/docs/en/models/rate-limits>
- Hugging Face Inference Providers pricing: <https://huggingface.co/docs/inference-providers/pricing>
- GitHub Models retirement: <https://docs.github.com/en/github-models>
- NVIDIA Kimi K3 hosted NIM: <https://build.nvidia.com/moonshotai/kimi-k3>
- Antigravity CLI headless mode: <https://www.agy.dev/docs/cli/headless/>
