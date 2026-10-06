# Phase 2 conversation and trace acceptance

Code-level regressions are passing. Live Ubuntu service and client acceptance
remain pending until the checks below have been run and their results recorded.
This checklist covers conversation and trace continuity, not every Phase 2 gate.
Evidence integrity, runtime tool addition and knowledge maintenance retain their
own acceptance requirements in the roadmap.

## Run against the existing service

After pulling the published changes on the Ubuntu server:

```bash
cd ~/.openjarvis/src
git pull --ff-only
sudo systemctl restart openjarvis-api.service
uv run python scripts/check_session_continuity.py
```

The script prompts for your OpenJarvis username/password and an existing project.
Use an Administrator account because the workflow inspects shared traces.
An API key alone does not grant trace access. See [Account roles](../user-guide/account-roles.md)
if you need to designate your first administrator with local server access. The password is entered
without echo and is not accepted as a command-line argument. The default server
is `http://127.0.0.1:8000`; the default model is `qwen3.5:9b`. Override these with
`--url` and `--model` if necessary. `--project-id` skips the project chooser.
`--timeout` bounds each model exchange (default 180 seconds).

The script creates a disposable login and one conversation titled
“Phase 2 acceptance …”. It runs three model exchanges: HTTP, WebSocket, and a new
WebSocket connection. It verifies the saved messages and their trace identities,
rejects a missing session, then revokes its login and checks that the open socket
cannot continue. A final cleanup attempt revokes the login if a check failed.
The test conversation and traces remain for inspection; existing conversations
are not selected or changed. The model may produce its normal configured memory
or telemetry side effects for these synthetic prompts.

Tracing must already be enabled in the service configuration. A trace lookup
returning HTTP 404 is a failed acceptance check, including when tracing is off.
The script does not enable tracing, install packages, or launch another server.

The JSON report contains a pass/fail status, completed checks, conversation ID,
trace IDs when verified, and login-cleanup status. It excludes passwords, tokens,
and assistant answer text. Exit code 0 means the automated checks passed; 1 means
a check failed. The conversation ID is included as soon as the test session is
created, even if a later check fails. Share this report to diagnose failures.
Do not interpret `device_acceptance: pending` as a failure of the automated checks.

## Verify the actual clients

Use the same OpenJarvis login and selected project on each device. All clients
must point to the centralized Ubuntu backend.

| Check | Expected result | Status |
|---|---|---|
| Browser | Open the labeled acceptance conversation; all six messages appear | Pending |
| Desktop/Tauri | Open that same conversation; the same six messages appear | Pending |
| Android browser/client | Open that same conversation; the same six messages appear | Pending |
| Cross-client update | Send a short message on Android, refresh/reopen on desktop and browser; the new exchange appears once | Pending |
| Reload/restart | Reload the browser and restart the desktop client; the conversation and new exchange remain | Pending |
| Diagnostics | Trace IDs from the report resolve on the server with matching conversation identity | Pending |

Record the server commit, client versions, date, automated report and each device
result before marking this acceptance gate complete. A server-only report cannot
confirm microphone behavior, desktop packaging, or Android UI behavior. Delete
the labeled conversation through the normal application controls when finished.

## Include task routing in the automated check

After reviewing current passing diagnostics and enabling a general/coding/analysis/
vision assignment in Settings, run the same workflow with an explicit task:

```bash
uv run python scripts/check_session_continuity.py --routing-task coding
```

This runs the disposable conversation through the assignment for all three exchanges.
It also requires each HTTP/WebSocket decision to match its persisted trace's actual
model and routing metadata. It does not run diagnostics, change assignments, enable
fallback or alter source/tool approvals. A passing automated report still leaves the
actual client checks pending. Full workstream checks are in
[Phase 2 completion](phase-2-completion.md).
