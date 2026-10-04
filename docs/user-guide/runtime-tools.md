# Runtime tools

Administrators can add text transformations from **Tools** in the navigation
menu. Definitions, approval decisions, revisions and audit history live in the
server's SQLite database. No template file editing or server restart is needed
after adding a tool.

1. Sign in with an administrator account. To grant the role locally, use
   `uv run jarvis auth set-admin --username YOUR_USERNAME` from
   `~/.openjarvis/src`, then sign in again.
2. Open **Tools**. Choose a unique name beginning with `custom_`, describe when
   Jarvis should use the tool, and choose its transformation.
3. Save the definition. Review the saved description, action and revision on
   its card, then select **Approve & enable**.
4. In a tool-enabled chat, ask Jarvis to use the new tool. For a managed agent,
   add the tool's name to that agent's configured tool list.

The initial adapter supports uppercase, lowercase, reverse, character count and
identity transformations. Every tool accepts a single string named `input`, with
a 32,768-character limit. These transformations do not access files, credentials,
network services or external facts. They declare no factual evidence capability.
Further installable tool types remain roadmap work.

Approval and capabilities remain separate. Runtime tools pass through the
existing ToolExecutor, including its capability policy, confirmation, taint,
bounded execution and tracing controls. Runtime approval is always enforced,
even if optional global management enforcement for built-in tools is disabled.

Editing a definition withdraws approval and disables the tool. Review and approve
the replacement before using it. Disable takes effect on subsequent execution
checks, including previously resolved instances in another server process;
already completed work is not undone. Re-enabling requires an unchanged approved
definition. Removal also blocks previously resolved instances. Audit events
remain after removal and record the administrator identity, action, revision and
time. The web view shows the most recent 100 events for the selected tool.

The default database is `~/.openjarvis/runtime_tools.db`. It can be relocated with
`security.runtime_tools_db_path` in the server configuration. Back it up with the
other server databases. Approvals survive restart only when the definition
fingerprint and trusted adapter validator version still match. Changing the
adapter's validation/execution contract requires a version bump and fresh review.

The navigation menu also includes **Logout**. It attempts to revoke the human
session on the server, clears saved authentication and reloads the client to stop
active streams and recreate account-bound state. If the backend is unreachable,
local sign-out completes after at most five seconds; server revocation requires
a reachable backend or session expiry.

## Ubuntu and client acceptance

After pulling and rebuilding the frontend, use the existing service. Sign in as
an administrator, add `custom_demo` with the uppercase transformation, save and
approve it. Ask Jarvis to call `custom_demo` with `input` equal to `Hello`; its tool
result should be `HELLO`. Confirm a new ordinary account can use the approved tool
but cannot open management APIs. Disable it, repeat the request, and confirm it is
unavailable. Edit to lowercase and verify it stays unavailable until reapproved.
Restart the service and verify the approved tool persists. Check logout and login
with a second account in browser, Tauri and Android clients. These are live
acceptance steps; offline regression tests do not establish device acceptance.
