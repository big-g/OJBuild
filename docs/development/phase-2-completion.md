# Phase 2 completion and deployed acceptance

Phase 2's agreed implementation scope is complete when its code regressions pass.
**Phase 2 remains open until the deployed acceptance checks below are recorded.**
An offline test run cannot establish Ubuntu service health, real provider behavior,
GPU capacity, or browser/desktop/Android behavior.

The project owner selected the three existing approved runtime adapters and reviewed
MCP connections as the Phase 2 tool scope on 2026-10-06. Additional adapters and
arbitrary local package installation belong to a later isolated installation
workstream. Parallel model execution, public/authenticated model-server adapters,
model-server custom TLS, text-to-3D pipelines, optional multi-model review, and
Apple clients are outside this completion gate.

## Prepare the existing Ubuntu service

Record the deployed commit, date and client versions. Back up the existing server
configuration and databases using your normal deployment procedure. Pull/build the
frontend using the repository's pinned Node/npm requirements. Preserve existing
extras while adding the required server dependencies:

```bash
cd ~/.openjarvis/src
git pull --ff-only
uv sync --inexact --extra server
sudo systemctl restart openjarvis-api.service
sudo systemctl status openjarvis-api.service --no-pager
uv run python scripts/check_session_continuity.py
```

Use the existing service; do not start a second API process. Inspect the journal if
startup fails. The server extra includes cryptography for encrypted vault storage.
The continuity command prompts for credentials, creates a labeled disposable
conversation, tests HTTP/WebSocket/reconnect and trace identity, then revokes its
test login. See [its full behavior and cleanup](phase-2-acceptance.md).

The new `behavior-v2` suite requires rerunning diagnostics and saving current passing
results for previously assigned tasks. Saved server activation is retained; old
single-question results cannot authorize an assignment. Diagnose a cold/busy GPU
before interpreting a bounded diagnostic failure as evidence of model quality.

## Acceptance matrix

Use two disposable ordinary users plus an administrator. Use labeled synthetic
sources, tools, projects and files; avoid changing existing production definitions.
Delete only those disposable resources through their normal account-owned controls.
Do not put passwords, recovery codes, API keys, tokens or private document text in
the acceptance report.

| Workstream | Deployed checks | Status |
|---|---|---|
| Accounts and roles | Ordinary account cannot configure shared models, runtime tools or MCP administration; administrator can. New login starts with its own clean history. Logout revokes the server session. | Pending |
| Account recovery | Prepare a one-use code while signed in; retrieve username from the logged-out screen; reset a disposable account's password; old logins/password/code fail and new password succeeds. Exercise local-admin recovery for an account without a code. | Pending |
| Conversations and traces | Run the continuity command with tracing enabled. Match all three distinct trace IDs, persisted history and user/project identity. Open the same conversation on browser, Tauri and Android; update/reload/restart without duplicates. | Pending |
| Personal and universal sources | User A's private source is hidden from B. An administrator-reviewed shared Weather source is available to both; each can opt out or use their own provider. Edits/credential rotation withdraw sharing approval; neither user gains another's private credential or documents. | Pending |
| Provider and credential connections | Test configured LAN endpoints and IMAP with your real providers; TLS checks remain enabled. Restart and confirm encrypted credentials and per-instance OAuth/token connections continue to work. Removed/disabled connections cannot be consumed. | Pending |
| API configuration | Configure a disposable JSON API with the right auth/header and mappings. Inspect bounded sample results before saving/syncing; confirm owned indexed data, attribution and withdrawal after disable/remove. Never record credential values in output. | Pending |
| Evidence integrity | Ask for current weather without approved evidence and expect a clear blocked/unavailable result. Add approved evidence, verify source attribution/freshness, then disable it and confirm the answer no longer treats it as authority. Tool observations without evidence authority do not satisfy the gate. | Pending |
| Knowledge maintenance | With disposable documents, verify multi-query retrieval and conflicting claims, review the conflict before applying a resolution, and ensure repeated ingestion/removal retains correct source ownership and provenance. | Pending |
| Runtime tools and MCP | Create/review/approve each existing adapter and a test MCP connection. Verify approved calls, edit withdrawal, restart restoration, disable/remove and logout. Tool-enabled chat, managed streams and scheduled ticks obey the same approval/capability rules. | Pending |
| Model routing | Run/review current diagnostics on your actual models. Configure manual and task choices; verify HTTP, managed streams, immediate/scheduled ticks and stored reasons. Test a primary transport outage with an explicitly reviewed fallback, then a generation failure: only preflight may select the fallback. Restore the primary; disable/edit a rule and confirm later calls block. | Pending |
| Output and files | Generate an answer that reaches a length limit and confirm continuation or a visible incomplete warning. Save a harmless script and STL to A's Files page; preview without executing, download identical bytes, and confirm B cannot list/preview/download/delete A's files. | Pending |
| Browser/crawler and sandbox | Install optional browser/crawler dependencies in the deployed environment; exercise bounded browser/crawler reads and existing sandbox approval/capability restrictions. Failure must be visible without widening privileges or inventing evidence. | Pending |

For an enabled task assignment, additionally run:

```bash
uv run python scripts/check_session_continuity.py --routing-task coding
```

This verifies each HTTP/WebSocket task decision against its stored trace's actual
model and routing metadata. It does not enable assignments, run benchmarks, change
source/tool approval or enable fallback. The JSON result still marks device
acceptance pending until those devices are checked.

## Record closure

For every row, record pass/fail, server commit, client version, timestamp and a short
sanitized observation or matching trace/report ID. Fix failed checks and rerun the
affected rows. Mark Phase 2 complete only when every in-scope row has passed on the
centralized Ubuntu deployment and required clients. Identity diagnostics and source
management foundations alone do not close the phase.
