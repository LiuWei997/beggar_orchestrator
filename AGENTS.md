# Repository rules

## Secret handling (mandatory)

- Never commit, stage, print, quote, log, upload, or expose API keys, access tokens,
  passwords, OTPs, cookies, authorization headers, or the contents of `docs/.key`.
- Never read `docs/.key` merely to inspect or summarize it. Read a specific value only
  when an authorized runtime integration needs it, and do not include the value in any
  command argument, exception, test fixture, screenshot, response, or tool output.
- Prefer the operating-system Keychain via `beggar-auth set` over plaintext storage.
  `docs/.key` is a local fallback only and must remain mode `0600` and Git-ignored.
- Before every commit, verify that ignored files remain ignored and inspect the staged
  diff for secrets. If a secret enters Git history, stop using it, revoke it immediately,
  issue a replacement, and then clean the history.
- Examples and documentation may contain variable names and obvious placeholders only,
  never credential-shaped sample values.
- Application logs may contain provider name, model, status, latency, request ID and token
  counts, but never request authorization data or raw secrets.

These rules override convenience and apply to every human, agent, script, test, and
workflow operating in this repository.
