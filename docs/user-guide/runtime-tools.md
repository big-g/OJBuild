# Runtime tools

Administrators can add text transformations and numeric formulas from **Tools** in the navigation
menu. Definitions, approval decisions, revisions and audit history live in the
server's SQLite database. No template file editing or server restart is needed
after adding a tool.

1. Sign in with an administrator account. To grant the role locally, use
   `uv run jarvis auth set-admin --username YOUR_USERNAME` from
   `~/.openjarvis/src`, then sign in again.
2. Open **Tools**. Choose a unique name beginning with `custom_`, describe when
   Jarvis should use the tool, and choose its type. Complete the fields supplied
   by that adapter.
3. Save the definition. Review the saved description, action and revision on
   its card, then select **Approve & enable**.
4. In a tool-enabled chat, ask Jarvis to use the new tool. For a managed agent,
   add the tool's name to that agent's configured tool list.

The text adapter supports uppercase, lowercase, reverse, character count and
identity transformations. Text tools accept a single string named `input`, with
a 32,768-character limit.

The numeric formula adapter accepts a formula and a comma-separated list of
1–8 variable names. For example, create `custom_celsius_to_fahrenheit` with
`value * 1.8 + 32` and variable `value`. After approval, a call with `value: 100`
returns `212.0`. Use `(value - 32) / 1.8` for the inverse conversion.

Formulas support numbers, declared variables, parentheses and `+`, `-`, `*`, `/`.
They are interpreted through a restricted syntax tree; no Python evaluation or
script execution occurs. Expressions are limited to 512 characters, 128 syntax
elements and 32 nesting levels. Inputs and numeric literals must be finite and
within ±10¹²; intermediate/results must stay within ±10²⁴. Division by zero,
missing/extra variables, booleans and numeric strings produce failed tool
results. Calculations use floating-point arithmetic, so normal rounding applies.

Both adapters operate only on supplied inputs. They do not access files,
credentials, network services or external facts, and declare no factual evidence
capability. Further installable tool types remain roadmap work.

The Tools form and saved-definition review cards use metadata from the server's
trusted adapter registry. Each adapter owns its configuration validation,
parameter schema, executable action and validator version. Existing text-only
definitions keep their original fingerprints and approvals; editing them through
the new form still requires review as usual. Unknown or damaged definitions are
unavailable for execution and can be repaired or removed from the web interface;
valid tools continue to load.

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

## MCP discovery contracts

Configured MCP servers are discovered across every `tools/list` page before
their adapters are exposed. Discovery rejects malformed/incomplete catalogs,
duplicate names and repeated or invalid continuation cursors. Limits are 32
pages, 1,000 tools, 256 KiB per canonical tool contract and 2 MiB across tool
contracts; each tool is limited to 20,000 JSON values and 32 nesting levels.
These limits apply after transport decoding, not to wire response buffering.
A failed discovery closes that server's connection and leaves other configured
servers available.

Input schemas must be objects and describe object arguments (legacy empty schemas
remain supported). Structural checks cover types, required fields, common nested
schema maps/lists and annotations. This is not full JSON Schema validation or
runtime argument validation. References must be local; discovery never fetches
schemas. Unsupported or malformed contracts must be corrected at the server.

When MCP tool management is enabled, approval now binds the full remote contract,
including descriptions, annotations and extensions. Existing managed MCP tools
need fresh approval after this upgrade. Subsequent unchanged discovery preserves
approval; changed contracts withdraw it on rediscovery. Returned schemas and
metadata are detached copies. Remote annotations remain untrusted: a read-only
hint cannot grant privileges, bypass approval or establish factual evidence.
This does not add automatic refresh or detect remote implementation changes
that leave the advertised contract unchanged. Web-managed connections additionally
check their live catalog before every invocation, as described below.

## Web-managed MCP connections

An administrator can open **Tools → MCP connections**, save a connection, select
**Discover / refresh**, review every tool's name, description, input schema,
untrusted annotations and contract digest, then **Approve catalog & enable**.
Approval covers the entire saved catalog. Saving and discovery never call a remote
tool or authorize its execution. Approved tools are available across accounts
without restarting Jarvis. Managed agents select the displayed Jarvis tool name;
aliases include the connection name, a readable remote-name fragment and a digest.
The `custom_mcp_` prefix is reserved for these tools.

Public HTTPS is the default, using standard ports without query strings or URL
credentials. Administrators can also explicitly authorize LAN HTTPS connections
as described below. Optional bearer tokens are encrypted in
the server vault and never returned through management APIs or prefilled in forms.
Leaving the token field blank while editing keeps it; explicitly removing it clears
it. Changing the endpoint, authorized addresses or TLS trust clears a retained token. Supplying a replacement token
binds it to the new endpoint. Every edit disables the connection and withdraws its
catalog approval. Missing/wrong encryption keys block unlocking and token replacement;
restore the original key backup, or explicitly remove the connection to start over.

**Allow approved tool calls without per-call confirmation** defaults off. Leave it
off for interactive CLI use with a confirmation callback. Browser chat and schedules
currently have no such callback and will block those calls. Enable the option only
when reviewing a connection for automated use. Approval, confirmation and capability
authorization are separate: tools require `tool:invoke`, remain external/tainted,
and still pass existing boundary, capability, confirmation, tracing and executor
checks. A server's read-only hint cannot change these permissions. MCP success
does not establish factual evidence; these tools declare no factual evidence kinds.

Before calling a tool, Jarvis opens a short-lived client, negotiates a supported
handshake protocol (2025-03-26, 2025-06-18 or 2025-11-25), verifies the complete live
catalog matches the approved snapshot, then checks current database approval again.
A changed catalog blocks invocation and withdraws approval; discover and review it
again. Unchanged refresh preserves approval. Disable and remove block previously
resolved instances, including in another server process. Already dispatched remote
work is not undone. Remote implementation changes with identical advertised
contracts cannot be detected by this check.

Each request resolves and pins policy-authorized DNS addresses with TLS verification. Redirects
and ambient proxies are not used, and tool POSTs are never retried automatically.
Responses are limited to 2 MiB; outbound JSON requests to 64 KiB. An operation shares
a 60-second I/O deadline across initialization, catalog scan and call, with existing
source socket limits; DNS lookup time is outside the socket deadline. Four concurrent
connection operations per manager are admitted, and at most 24 connections can be
saved. Clients close after each operation and attempt bounded session termination
when the server supplies a session ID. Failed or malformed results propagate as
tool failures. Configured bearer-token reflections are rejected rather than stored
in catalogs or returned in tool results. Text content is exposed; richer MCP content
and provider-specific factual evidence validation remain follow-ups.

The MCP database is beside the configured runtime tool database: the default is
`~/.openjarvis/runtime_tools_mcp.db`, with its encryption key in
`runtime_tools_mcp.key`. A custom `example.db` runtime tool path uses the sibling
`example_mcp.db`/`example_mcp.key`. Back up both the database and key; the key is
essential to restore tokens. Audit records contain actor, action, revision and time,
remain after removal, and show the latest 100 events per connection.

Legacy MCP configuration remains supported separately and is not automatically
imported or approved. The web interface supports selective legacy public HTTPS
imports; imports remain disabled and unapproved. Configure legacy LAN entries
manually with exact address authorization. Local package installation and the
newer handshake-free protocol remain subsequent roadmap work.

## Authorized LAN MCP endpoints

In **Tools → MCP connections**, select **Authorized private LAN**, use a URL such
as `https://mcp.internal:8443/mcp`, and list 1–32 exact private addresses such as
`192.168.1.20, fd00::20`. Only RFC1918 and IPv6 unique-local addresses are accepted;
subnets, loopback, link-local, metadata and public addresses are rejected. Every
DNS answer must be listed, including all IPv4/IPv6 answers. Each request resolves
again and connects to one verified IP while retaining the URL hostname for Host,
TLS SNI and certificate verification. Redirects, proxies and POST retries remain
disabled. Configuration performs no network request.

LAN connections may use custom HTTPS ports. Plain HTTP is not supported. Choose
**System trust** for normally trusted certificates, or **Private CA certificates**
and paste only PEM CA certificates (up to eight certificates / 16 KiB). This trust
applies only to that saved connection and replaces system roots for it. Private
keys and leaf certificates are rejected. The server certificate must match the URL
hostname; using a literal IP requires an IP subject alternative name. There is
no skip-verification option.

Saving this setting authorizes discovery traffic at the listed endpoint, including
its supplied bearer token, but never tool execution. Discover and review the full
catalog before approving. Any configuration edit disables the connection and
withdraws approval; endpoint, address or trust changes also clear retained tokens
unless a replacement is explicitly supplied. Re-discover and approve before use.

For live acceptance, use an MCP server you control on the LAN with valid TLS.
Check discovery without calls, administrator approval, restart persistence, DNS
moving to an unlisted address, wrong-host/untrusted certificates, token rotation
and disable/revocation from browser, Tauri and Android. Use the existing Ubuntu
service. Offline tests do not establish these deployment/provider checks.

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

Also create the Celsius-to-Fahrenheit formula above, verify its `212.0` tool
result, edit the formula and confirm it stays unavailable until reapproved.
Restart the service and verify both the original text tool and the formula still
honor their current approvals. Failed formulas must surface a tool error rather
than provide fabricated results.

For MCP acceptance, use a public HTTPS MCP endpoint you operate or trust. Verify
saving makes no request, discovery makes no tool call, and unapproved catalogs
cannot execute. Review and approve it with the desired confirmation setting, then
use its displayed tool name from chat and a configured managed agent. Disable it
and verify a previously selected tool is blocked. Rotate the token or change the
endpoint and confirm fresh discovery/approval is required. Change a remote tool's
schema/annotations and confirm Jarvis blocks the call before dispatch and withdraws
approval. Restart and repeat from browser, Tauri and Android. Provider/device
acceptance remains pending; offline fake-server regressions are not live acceptance.
