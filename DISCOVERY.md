# Interface discovery

Tested on Windows with `codex-cli 0.163.0-alpha.11.1` (2026-10-23).

- `codex exec --json` supplies `thread.started.thread_id`, `item.completed`
  agent messages and `turn.completed`/`turn.failed`. Python owns the exit code.
- `codex exec resume <exact UUID> --json` is non-interactive. Resume accepts
  `-c sandbox_mode="read-only"` / `"workspace-write"` and
  `-c approval_policy="never"`; no conversation database is needed.
- No standalone usage command was found. The documented account-only stdio
  request `account/rateLimits/read` was verified without starting a model turn.
  This short-lived CLI subprocess is used only for usage, not task execution.
  No Bridge daemon, listening socket or custom authentication is introduced.
- Local `sessions/**/*.jsonl` contains `event_msg` / `token_count` /
  `rate_limits`. Window durations 300 and 10080 identify the two quotas.
  The account RPC uses `windowDurationMins`; neither reader relies on slot names.
- A tiny read-only exec probe is a fallback for unsupported account interfaces.
  Incompatible data remains UNKNOWN; snapshots past their reset cannot be fresh.
- The supplied legacy Skill was read as a behavior specification. Reference
  material also contained source excerpts; none is incorporated in this project.

Sources: [non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode),
[account interface](https://learn.chatgpt.com/docs/app-server), and local CLI help.
These interfaces, especially alpha CLI versions and session files, can change.
