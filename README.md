# Codex Bridge

[English](README.md) | [简体中文](README.zh-CN.md)

A lightweight local bridge for governed, resumable delegation to the OpenAI Codex CLI.

Give it a project and a task. It checks your available Codex capacity, starts the
local CLI in the background, and saves progress and results. Continue later using
the same native Codex session, even after your calling agent has disconnected.

**Codex Bridge does not include Codex.** You need Python 3.10+ and an already
installed, authenticated [Codex CLI](https://learn.chatgpt.com/docs/cli).
The bridge uses only the Python standard library and works independently of any
particular agent host. Version **1.0.0**, MIT license.

## Get started

```powershell
git clone https://github.com/AlexLI1935/codex-bridge.git
cd codex-bridge
codex --version
python codex_bridge.py init
python codex_bridge.py environment
python codex_bridge.py usage-refresh
```

If authentication is required, run `codex login` yourself and retry. Codex manages
its own login and token refresh. The bridge never opens or copies authentication
files and does not implement OAuth. On Windows, use a native `codex.exe`; if PATH
selects an npm `.cmd` wrapper, set `CODEX_BRIDGE_CODEX` to the native executable.

Start a task, replacing the project placeholder with an existing absolute path:

```powershell
python codex_bridge.py run --workdir "<absolute-project-path>" --mode write --prompt "Fix the failing test, run the suite, and report the changes."
python codex_bridge.py status <task-id>
python codex_bridge.py resume <task-id> --prompt "Add a boundary test and run it."
python codex_bridge.py cancel <task-id>
```

`run` and `resume` return immediately with a saved task ID. Use `--wait` for
foreground execution. `status` only reads local state; polling does not query
usage. Cancellation is a durable request, normally processed within a fraction
of a second. It does not undo edits. A cancelled task can be explicitly resumed.
For long prompts, use `run --prompt-file <file>`.

The default mode is `read_only`, suitable for review and investigation. `write`
selects Codex's `workspace-write` sandbox with non-interactive approvals.
`--mode full` explicitly selects `danger-full-access`; use it only when the user
has approved that scope. The bridge never escalates automatically. Sandbox
availability and enforcement depend on Codex, the OS and managed policy.
Review an existing checkout or create a Git worktree yourself and pass its path;
the bridge does not create branches, push or deploy on its own.

## Capacity and waiting

Before each actual start or resume, the runtime loads validated config and
requires fresh readings for **both** subscription windows. Defaults stop new
starts at **90% five-hour** or **95% weekly** utilization, aiming to leave about
10% / 5% for manual work. These are start gates, not a hard billing cap; an
already running task can consume more capacity between provider updates.

`usage` reads local data. `usage-refresh` first tries the documented, non-generative
`account/rateLimits/read` interface through a short-lived local `codex app-server`
stdio process. It does not start a model turn or a persistent server. If that
interface is unsupported, a tiny read-only `exec` probe runs outside your project,
then the bridge rereads local session snapshots. This fallback uses a small
amount of quota. No TUI scraping or earned-reset redemption is used.

Readings are reused for less than five minutes by default. Refresh attempts share
a persisted cooldown of the same duration. Stale or unknown data triggers one
refresh and reread; unresolved data is reported honestly and blocks dispatch.
Windows are identified by duration (300 / 10080 minutes), independent of slot
names. One available window is reported without inventing the other; automatic
starts still require both. A reset invalidates an older snapshot.

Usage access depends on the current local CLI/account/session behavior, **not a
guaranteed permanent public billing API**. Changed schemas return UNKNOWN.
API-key-only accounts may not expose subscription windows. Keep one runtime home
per account, and refresh after switching accounts.

At a threshold, the task becomes `WAITING_USAGE_RESET` with the actual provider
reset, blocked window(s), utilization and local display. If both windows block,
the later reset wins. `resume_after_epoch` includes the configured grace period.
After that time, call `resume`; it checks usage again before launching. There is
no automatic wake service: a caller may arrange its own later invocation.

Quota exhaustion during execution also preserves a waiting task and exact native
session ID. Resume continues that session rather than replaying the original
prompt. If the CLI reports no usable absolute reset, it stays null; refresh
later instead of assuming “five hours from now”. Missing session IDs require
human attention. An authentication error is reported as `AUTH_REQUIRED`.

## Settings and time

Runtime files live outside the checkout:

- Windows: `%LOCALAPPDATA%/codex-bridge`
- Other systems: `~/.codex-bridge`
- Override with `CODEX_BRIDGE_HOME`; locate the CLI with `CODEX_BRIDGE_CODEX`.

`config.json` is authoritative. [config.example.json](config.example.json) is a
template. Configuration contains policy and time observations, never credentials.

```powershell
python codex_bridge.py config show
python codex_bridge.py config validate
python codex_bridge.py config set usage.five_hour_max_pct 85
python codex_bridge.py run --workdir "<absolute-project-path>" --prompt "Review this change." --profile complex --model <model-id> --reasoning-effort high
```

Models default to `null`, inheriting your Codex model; profiles set reasoning
effort (`medium` / `high`). Per-task overrides do not change your global settings.
Direct config edits are supported; unknown fields and invalid values are rejected.

`init` or the first dispatch observes an external HTTPS UTC Date and local wall
clock, recording their difference as `observed_utc_offset_minutes`. It also
records the OS timezone label without guessing geography. A valid baseline is
reused across starts. Timezone/offset changes, clock discontinuity or
`init --refresh-time` trigger a new probe. Failed probes report UNVERIFIED and do
not invent an observed offset. UTC disagreement over 120 seconds reports
`CLOCK_SKEW`; fix the system clock before running.

Reset and waiting truth stays **UTC / epoch**; timeouts use a monotonic clock.
The observed offset is only for current local display. It is not a future DST
rule. The bridge has no local calendar recurrence engine.

## Results and process safety

Tasks persist as `tasks/<id>.json`. Attempts keep `stdout.jsonl`, `stderr.log`,
`report.md` and a hash receipt under `results/<id>/attempt-N/`. Keep this directory
private: prompts and model/tool output can contain project information.

`COMPLETED` requires exit code zero, a valid JSON stream, a completed turn and a
final response. `report_validated` checks the stored report and receipt. This
does not prove the code is correct: inspect the diff and test the result.
States also include `QUEUED`, `RUNNING`, `FAILED`, `NEEDS_INPUT`,
`WAITING_USAGE_RESET` and `CANCELLED`. Unexpected runner termination becomes
`NEEDS_INPUT`; work is never silently replayed.
On POSIX, force-killing the runner can leave children. Inspect and stop its owned
tree, then clear `cleanup_required` in the saved task before explicitly resuming.

Each task has a kernel-backed lock. Different tasks can run concurrently, ideally
in separate worktrees. Cancellation and timeout target only the process tree the
runner created: a Windows Job Object or a POSIX process group. No process-name
scan or “kill all Codex” is used, so an unrelated Desktop Codex is not a target.
Idle diagnostics compare output, the task's session file and workspace activity;
they never kill a task merely for quiet output. The explicit timeout still applies.

CLI exit codes: 0 accepted/read/completed; 1 a non-successful task state;
2 an operation/argument error. Read a background task's saved status to learn its
outcome. [SKILL.md](SKILL.md) provides the agent operating contract.

## Development

```powershell
python -m unittest discover -s tests -v
```

Offline tests use synthetic events and owned child processes, without paid model
calls. Windows live smoke covered background write, same-session resume and
read-only enforcement. Linux/macOS process-group behavior needs platform validation.
[DISCOVERY.md](DISCOVERY.md) records the tested CLI and public interface sources.

## Author

Created and maintained by [Alex Li](https://github.com/AlexLI1935).

[MIT License](LICENSE), copyright (c) 2026 Alex Li.
