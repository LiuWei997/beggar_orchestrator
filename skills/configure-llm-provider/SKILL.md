---
name: configure-llm-provider
description: Configure and verify a legally obtained LLM provider API credential for a beggar-orchestrator project. Use when adding, rotating, checking, or removing a provider credential; do not use for account farming or bypassing signup verification.
---

# Configure an LLM provider

Inspect the project's `docs/providers.md`, `docs/authentication.md`, and provider TOML before changing configuration.

Keep account creation a user-controlled step. Give the official signup or API-key page when needed, but do not automate CAPTCHA, phone verification, real-name verification, terms acceptance, payment activation, or creation of multiple accounts for promotional credit.

Never ask the user to paste a secret into chat and never place one in a command argument, file, log, or tool output. Have the user run `beggar-auth set <provider>` in a local terminal so hidden input writes directly to the operating-system keyring. Pause if interactive secret entry is not available.

After the credential is stored, use `beggar-auth verify <provider> --config <path>` and a minimal, low-token model request. Report the provider, endpoint, tested model and result without revealing the credential. Enable the provider only after verification succeeds.

For rotation, store and verify the new credential before invalidating the old one when the provider permits overlapping keys. For removal, identify dependent routes before calling `beggar-auth delete <provider>`.
