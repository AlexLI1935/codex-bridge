# v1.0.0 validation

Validated on 2026-10-23 with Python 3.12 on Windows and
`codex-cli 0.163.0-alpha.11.1`.

## Offline: 60 tests passed

Tests exercise validated config, cache freshness and unknown states, both quota
durations, partial windows, malformed/changed JSONL, UTF-16 snapshots, official
account response normalization, refresh fallback and cooldown, 90/95 gates,
later-reset selection, task completion/failure, timeout/cancel, session capture,
native resume command construction, restart recovery, duplicate dispatch,
independent concurrent tasks, report receipts, clock/offset changes and CLI JSON.
Additional Windows regressions cover an open reader during file replacement,
transient sharing violations and concurrent task-state reads/writes.

The process isolation test launches real Python processes: it kills an owned
parent and child while leaving an unrelated process running. Windows uses the
same Job Object mechanism for the live Codex CLI. No Desktop Codex process is
selected for termination.

Mid-run quota exhaustion and structured reset handling are tested with synthetic
CLI events, not by burning real quota. No live quota exhaustion is claimed.

## Live Windows smoke: passed

- Background workspace-write created an add function and two passing tests.
- Native resume preserved the exact session ID, added a negative-number case
  and ran three passing tests. The bridge stored no conversation history.
- A separate read-only review attempted an authorized marker write; the command
  returned PermissionError, no marker existed, and the review completed.
- Temporarily lowering the ceiling to zero produced WAITING_USAGE_RESET before
  any Codex process started. The default ceiling was restored to 90 afterwards.
- The newly implemented account-only refresh returned fresh five-hour and weekly
  windows without a model turn.
- Initial time observation succeeded and was persisted; subsequent unchanged
  environment checks reused it.

Live testing exposed recoverable WebSocket errors followed by Codex's successful
HTTPS fallback. Completion classification now distinguishes those warnings from
terminal errors; an offline regression test protects that behavior.

## Publication checks

The Skill passes the Skill Creator validator. Configuration matches its example.
The source tree contains no runtime files, auth files, real session history,
private machine paths or detected credential strings. Implementation provenance
is recorded in [PROVENANCE.md](PROVENANCE.md).

## Limits

- No wake daemon, scheduler, MCP server or notification service is included.
  A caller invokes resume after the absolute reset plus grace and fresh preflight.
- API/session formats may change. Unknown or missing quotas block dispatch.
- Real quota exhaustion was deliberately not induced; that path is mocked.
- Codex model execution and sandbox behavior were live-tested on Windows only.
- A force-killed POSIX runner can leave children; inspect its owned tree before
  clearing the recovery guard and explicitly resuming.
