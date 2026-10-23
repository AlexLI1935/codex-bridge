---
name: codex-bridge
description: Delegate authorized coding, debugging or review tasks to a local Codex CLI with usage checks, durable results and native session resume.
license: MIT
metadata:
  author: Alex Li
  homepage: https://github.com/AlexLI1935/codex-bridge
  repository: https://github.com/AlexLI1935/codex-bridge
---

# Codex Bridge

Use when the caller asks or authorizes delegation to the local Codex CLI.
Ordinary coding explanations do not need delegation. Requires Python 3.10+ and
an installed, authenticated Codex CLI; Codex manages its own authentication.
Resolve `codex_bridge.py` relative to this Skill directory, then invoke it using
an absolute path and the appropriate Python interpreter.

## Preflight and dispatch

Run `python <bridge>/codex_bridge.py init` once, then inspect
`usage`. For UNKNOWN or STALE, call `usage-refresh` and reread `usage`. Do not
declare Codex unavailable before attempting refresh; do not equate UNKNOWN with
any percentage. A refresh can use a tiny model probe if the account interface is
unsupported. Reuse fresh cache and respect the refresh cooldown.

```text
python <bridge>/codex_bridge.py run --workdir <absolute-project-path> --mode write --prompt-file <task-file>
python <bridge>/codex_bridge.py status <task-id>
python <bridge>/codex_bridge.py resume <task-id> --prompt "Add the missing boundary test and run it."
python <bridge>/codex_bridge.py cancel <task-id>
```

The runtime independently loads validated persistent config and checks both
windows before every actual start/resume. Default ceilings are 90% five-hour and
95% weekly, leaving about 10% / 5% for manual work. Change them only under the
user's requested policy using `config set`, then `config validate`. A Skill or
agent memory is not the enforcement authority.

`run`/`resume` execute in the background without a PTY; retain the task ID.
`--wait` is optional foreground mode. `status` does not send usage requests.
Do not use shell sentinels or terminal notifications as completion authority.

## Interpret results

Read the saved `status`, `reason`, `exit_code` and `report_validated`.
`COMPLETED` means the process and structured completion checks passed, not that
the code is necessarily correct. Read `report_path`, inspect changes and run
appropriate checks before presenting the outcome.

For `WAITING_USAGE_RESET`, show the blocked windows and actual reset. The later
reset wins if both block; `resume_after_epoch` includes grace. Arrange a later
call yourself if authorized; there is no automatic wake service. At that time,
`resume` performs usage preflight again and uses the exact original Codex session.
If reset is absent, report that honestly and refresh later. Never substitute
“now + five hours” for a provider reset.

`NEEDS_INPUT` / `AUTH_REQUIRED` requires the user to run `codex login`.
For other NEEDS_INPUT reasons, inspect the reason before acting. If a session is
missing after execution began, do not replay the original task automatically.
Cancellation does not undo edits. Explicit resume can continue cancelled work.

## Models, permissions and process safety

Default model `null` inherits the user's Codex model. Use `--profile complex`,
`--model` or `--reasoning-effort` as appropriate for the requested task; there is
no embedded model routing table. Do not change global Codex config.

Use `read_only` for reviews and audits, `write` for approved project changes.
Write uses the native workspace sandbox with unattended approvals. `full` is
only for explicitly authorized wider access, including a user-approved response
to a demonstrated sandbox limitation. Never silently escalate.
For independent changes, let the caller prepare separate Git worktrees and pass
each workdir. Do not push, deploy or change system services/schedules without
the relevant explicit instruction.

Cancel through the bridge. It owns its child process tree; never kill by process
name, scan for all Codex processes, or terminate Desktop Codex. A suspected stall
is diagnostic, not permission to kill unrelated processes or replay work.

Internal reset/wait truth is UTC/epoch; model-perceived time has no role.
The cached observed local offset is for display, not geographic inference or
future DST rules. Use `init --refresh-time` for an explicit new observation.
Keep runtime config, logs and session output private and outside the repository.
