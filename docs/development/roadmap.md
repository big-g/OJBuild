# OJBuild Roadmap

This section governs the customized `big-g/OJBuild` project. The upstream
workstreams below are reference material, not completion criteria for this build.

## Design requirement: dynamic configuration and growth

New configuration-heavy features must support database-backed instances managed
through the web frontend. Separate adapter/tool definitions from configured
instances; definitions declare versioned settings, validation, operations, and
capability requirements. Avoid one hardcoded connection per integration.
Registration and configuration never imply authorization. Preserve the existing
ToolSpec/ToolExecutor, capability governance, provenance, and evidence hard gate.
Use explicit migrations and independent identities for configuration, sync state,
and indexed data. Extensibility must retain bounded execution and observable errors.

## Design requirement: understandable configuration

Configuration forms must provide visible format/range hints and realistic examples
next to constrained fields, with accessible associations to their inputs. Explain
unfamiliar terms and multi-step setup in expandable help available within the app.
Validation failures must explain how to correct the input without echoing secrets.
MCP connection setup now supplies naming rules/examples, actionable name errors
and an in-app guide covering endpoints, tokens, LAN trust and catalog approval.
Apply this standard as other configuration forms are added or revised.

## Project phases

| Phase | Status | Scope |
|---|---|---|
| 1 | Complete | Browser voice conversation, speech recognition, speech playback, resume listening |
| 2 | Active | Authentication; persistent conversations across browser, desktop and Android; knowledge/reasoning and evidence integrity; tool governance and runtime tool addition; trace correlation; centralized Ubuntu backend and client validation |
| 3 | Planned | Text-to-image and text-to-3D generation, followed by secure remote access |
| 4 | Planned | Home Assistant integration; Echo devices as Jarvis clients in Phase 4.1 |
| 5 | Planned | Nextcloud monitoring/notifications and a self-hosted email server |

Account-owned conversations, source access and generated files are in Phase 2.
Operating-system isolation for privileged code tools and Apple client work remain
outside the current Phase 2 scope.
Database-backed model configuration and configurable task routing are now a
bounded closing Phase 2 objective. Existing explicit model selection remains
supported. Multi-model collaboration and parallel execution are later extensions.

## Phase 2: model configuration and task routing

**Agreed scope (2026-10-05):** preserve InferenceEngine, EngineRegistry,
MultiEngine and the model catalog. Add database-backed model-server connections
with administrator web configuration, separate model serving identity from
connection identity, and support multiple Ollama servers from the beginning.
Configure explicit task assignments for coding, analysis, vision and general
conversation using measured performance and verified capabilities. Keep manual
selection available; record routing reasons and bounded fallback in traces.
Routing must preserve tool governance, evidence integrity and account boundaries.
Discovery, user-assigned labels and model registration never grant capabilities.

**Implemented first batch:** administrator Settings model-server add/edit/remove,
revision checks, retained audit history and dedicated versioned SQLite storage.
Ollama connections support loopback and explicit private LAN IP root URLs.
Bounded catalog tests reject redirects, proxies, oversized/malformed catalogs
and duplicate serving IDs. Per-connection catalogs persist across restarts;
edits/failures invalidate snapshots, and stale tests cannot overwrite new settings.
Catalog presence is distinct from verified capability/inference availability.
Saving/testing neither changes current inference routing nor runs/pulls models.
See [Model server connections](../user-guide/model-connections.md).

**Next:** integrate explicit selection across saved servers and capability/health
validation, then configurable task rules, benchmark-informed assignments,
fallback and trace explanations. Live Ubuntu/browser validation remains required.
Authenticated/public inference endpoints and custom TLS trust are adapter
extensions. Shared-agent isolation, resource limits and load testing precede
parallel execution. Phase 3 extends routing to image/3D workflows and optional
multi-model review without making collaboration a Phase 2 completion gate.

## Phase 2: complete output and generated downloads

**Implemented:** authenticated chat respects the request's output-token budget,
restores shared-agent settings on success or failure, and preserves provider
length-limit signals through agent and direct streams. Simple and function-calling
orchestrator responses attempt at most two continuations and include their token
usage. The chat UI retains an incomplete-output warning when the provider still
reports `length`; model context/output limits still apply.

**Implemented:** a dedicated, configurable server file directory with database
ownership, immutable blob IDs, SHA-256 integrity checks, per-user quotas and
non-executable file permissions. Authenticated users can create, list, preview,
download and delete only their own files; administrators do not bypass ownership.
The verified chat identity binds `artifact_save` through the existing tool executor,
capability and approval gates. Code blocks also provide an explicit Save file action.
The Files page displays escaped text, bounded binary summaries and STL wireframes
without executing scripts or rendering active documents.

See [Generated files](../user-guide/generated-files.md) for configuration and limits.
This supplies file delivery for generated scripts/STL content; it does not implement
the Phase 3 text-to-3D model pipeline. Live Ubuntu/browser/desktop/Android acceptance,
content scanning, retention policies, larger-file streaming and additional inert
preview formats remain follow-ups.

## Phase 2: account roles and system administration

**Implemented:** explicit User/Administrator creation roles in the local CLI and
admin-only Settings account management. User is the default; upgrades preserve
existing roles. The backend requires a live human administrator for shared system
management, including keyless installations and shared agent event streams.
API keys and client-side role flags do not grant administrator authority. Role
changes revoke affected login sessions; web administration prevents self-removal
or self-demotion and checks actor authority again inside write transactions.
Personal projects, chats, files and named sources retain their ownership checks.
See [Account roles](../user-guide/account-roles.md) for administrator bootstrap and
permissions. Live Ubuntu/browser acceptance remains required.

## Phase 2: account recovery

**Implemented:** login-screen username retrieval/password reset using a high-entropy
recovery code; Settings → Account password changes and recovery-code issuance
require the current password plus a verified human session. Recovery codes are
hashed in the database, expire after 30 days, and are atomically consumed by a
password reset. Replacement/password changes invalidate old codes, and password
changes/reset revoke existing login sessions while preserving the user identity.
Local `auth list-users`, `auth recovery-code` and `auth reset-password` provide
administrator recovery when the user is already locked out with no saved code.
See [Account recovery](../user-guide/account-recovery.md). Browser/client runtime
acceptance remains pending; no email delivery service is assumed.

## Phase 2: source ownership and universal access

**Implemented:** personal named-source ownership, explicit administrator roles,
sharing requests and administrator-approved universal connections; web sharing
and per-account use controls. Metadata-only consumer cards; owner/administrator
management checks for settings, credentials, OAuth, sync, schedules and audits.
Changes to approved configuration/authorization withdraw shared approval.
Named-source live reads, keyword/hybrid retrieval and SQL aggregation honor
ownership, current approval and per-account selection. Legacy controls require
administrator access; migration recovers proven creators and leaves unowned
sources for administrator review. Existing indexes/checkpoints/vaults persist.
See [Personal and universal sources](../user-guide/shared-sources.md).
**Pending:** runtime validation of personal/shared Weather connections across
accounts, including approval, withdrawal and per-account use selection.

**Confirmed by user (2026-10-04):** new-account login succeeds after server
extras were installed in the service environment. Account/backend chat-cache
isolation and health probes using the configured API URL resolve the stale chat
list and false backend-warning reports. Recovery and device-continuity acceptance
remain open.

## Phase 2: runtime tool approval integrity

**Implemented:** template approval fingerprints include a canonical digest of the
executable action. Editing a command, expression or transformation at the same
source path withdraws approval on rediscovery; live definition changes are blocked
by the existing ToolExecutor management gate. Loaded definitions and returned
parameter schemas are detached from caller-owned dictionaries. Unchanged reloads
preserve approval. Existing managed templates require one fresh approval after
this fingerprint upgrade. Regression coverage includes file reloads, all three
action types, live mutation and approved execution.

**Implemented:** first database-backed runtime tool adapter with administrator web
add/edit/review/approve/disable/enable/remove controls and retained audit history.
Bounded declarative text transformations have fixed ToolSpecs and no factual
evidence claims. Approved definitions are loaded for tool-enabled chat on each run
and resolve by name in managed-agent streaming and scheduled execution. Runtime
approval is enforced independently of optional built-in management enforcement;
live database checks block stale instances after edits, disable or deletion.
Revision checks reject stale reviews; matching definitions and trusted validator
versions restore approval after restart. Navigation now provides Logout with
bounded server revocation, local authentication cleanup and client reload.
See [Runtime tools](../user-guide/runtime-tools.md).

**Implemented:** trusted runtime adapter metadata registry with web forms and
saved-definition review cards driven by adapter fields. Numeric formula tools
support declared numeric inputs and bounded arithmetic for calculations such as
unit conversions; restricted syntax interpretation, finite numeric limits and
failed-result propagation preserve the existing executor and evidence contract.
Per-adapter validation versions bind approval independently; original text-tool
fingerprints and approvals remain compatible. Unknown/damaged definitions are
excluded from execution while administrators can repair or remove them without
blocking valid tools.

**Implemented:** complete, bounded MCP catalog discovery before adapter creation.
Pagination rejects duplicate names, repeated/invalid cursors, malformed entries
and catalogs beyond page/tool/size/structure limits without partial registration.
Object input schemas receive structural checks; remote schema references are
rejected and no references are fetched. Full remote contracts, including
descriptions and untrusted annotations, bind managed approval fingerprints.
Detached specs prevent caller mutation; failed discovery closes its connection.
Annotations do not grant capabilities or factual evidence authority. This is
discovery hardening, not a full JSON Schema validator or runtime MCP installer.

**Implemented:** administrator web management of database-backed public HTTPS MCP
connections: add/edit/remove, encrypted origin-bound bearer tokens, complete
catalog discovery/review, revision-bound approval, enable/disable and retained
audit history. Saving/discovery never authorize calls. Approved tools refresh in
existing runtime chat/managed/scheduled paths; mandatory live database checks
block stale instances, and changed remote catalogs block invocation and withdraw
approval. Explicit local confirmation settings remain independent of server hints
and capability grants. Pinned requests reject private DNS, redirects, credential
reflection and implicit POST retries. Short-lived clients terminate sessions on
close when possible. Offline regressions cover lifecycle, races, network guards
and administrator/secret boundaries; live acceptance remains pending.

**Implemented:** administrator review and selective import of server-configured
legacy MCP entries into database-backed connections. Opaque review handles bind the
exact configuration and credentials; changed sources/restarts require fresh review.
Imports remain disabled, undiscovered and unapproved, with confirmation required;
bearer tokens enter the encrypted vault and audit provenance records the importer.
Name collisions and repeated/concurrent imports never overwrite saved connections.
Commands, unsupported settings and tool filters are blocked rather than silently
executed or broadened. Legacy files/settings remain unchanged and must be disabled
or removed before activating replacements. No remote connection occurs during import.

**Implemented:** per-connection LAN MCP settings managed by administrators in
Tools: exact RFC1918/IPv6 unique-local addresses, HTTPS including custom ports,
and optional bounded PEM private CA trust. Saving explicitly authorizes catalog
discovery at that endpoint; tool calls still require revision-bound catalog
approval and existing capability/confirmation gates. Every DNS answer must be
listed, with per-request resolution and address pinning. Public/mixed/unlisted,
loopback, link-local and metadata destinations fail closed; certificate and
hostname verification remain mandatory. Endpoint/address/trust edits withdraw
approval, clear discovery and clear retained tokens unless explicitly replaced.
Existing public definitions and approvals retain their public/system-trust defaults.
Offline DNS/TLS/lifecycle regressions pass; live LAN/provider acceptance remains pending.

**Implemented:** per-connection MCP authentication choices in the administrator
web interface: Bearer tokens or a provider-specific API key header. The existing
encrypted vault binds secrets to endpoint and authentication settings; method/
header edits clear retained credentials and withdraw catalog approval. Header
validation blocks routing/transport/protocol overrides and injection. Static
credentials are applied consistently to discovery, calls and session termination;
metadata-only management/review and secret-safe failures remain intact. Default
Bearer definitions, legacy ciphertext and approvals remain compatible. MCP OAuth
consent, token exchange and automatic refresh remain separate future integrations.

**Implemented:** a third database/web-managed runtime adapter for JSON field
extraction from supplied text. Saved paths, visible examples and review cards
support nested object keys and zero-based array indexes with explicit escaping.
Strict input/byte/depth/value/output limits, duplicate-key/nonfinite-number
rejection and failed missing-path results prevent ambiguous partial output.
Existing approval fingerprints, restart restoration and live revocation checks
cover path edits and loaded instances. Extracted data has no factual evidence
authority; no network, file access or expression execution is introduced.

**Pending:** further installable tool adapters and local package installation;
live Ubuntu/browser/Tauri/Android validation of installation, restart, revocation
and logout. Implemented adapters do not complete runtime tool addition acceptance.

## Phase 2: dynamic source management

1. **Implemented:** database-backed, named source instances;
   adapter-provided configuration fields and server validation; web add/edit/test/
   enable/disable/sync/remove controls; independent indexing and checkpoints;
   migrate the initial Local Files JSON connection without duplicating its index.
2. **Implemented:** public Web Page and JSON API GET adapters through the same
   instance contract. Public destinations are checked and pinned per redirect;
   response sizes and record counts are bounded; JSON mapping supports stable IDs
   and explicit complete-snapshot reconciliation. Connection tests fetch/parse
   without indexing; evidence retains actual fetch time and original/final URLs.
   **Implemented:** protected credential references for bearer/API-key HTTPS GET
   connections; encrypted server vault and web add/rotate/remove controls; immutable
   origin/header bindings; metadata-only APIs; same-origin authenticated redirects;
   credential-use capability requirements; in-use/reference-safe lifecycle checks.
   **Implemented:** explicit Data Sources → Add API connection entry point for
   non-AI service data, with in-app setup/mapping/analysis guidance and metadata-
   driven field hints/limits. Connection tests expose up to three bounded,
   escaped mapped-data previews only after full scan validation and credential-
   reflection checks; tests do not persist/index data and form edits clear stale
   previews. Saved APIs reuse personal/universal ownership, encrypted credentials,
   schedules and attributed knowledge retrieval for intelligent feedback.
   See [Service APIs](../user-guide/service-apis.md). Live provider acceptance,
   private LAN generic APIs, general OAuth, other response formats and write
   operations remain follow-ups; scheduled ingestion is not automatic alerting.
   **Implemented:** bounded same-origin next-URL/cursor pagination; server-issued
   incremental sync tokens committed only after ingestion and cleanup; explicit
   deletion-ID arrays scoped to one instance; complete-snapshot reconciliation
   across all pages. Failed/truncated scans retain the prior token and watermark.
   Private-network services and named-source OAuth adapters remain separate
   future integrations.
3. **Implemented:** persistent opt-in interval schedules and queued sync runs;
   authenticated web progress/cancellation controls; bounded per-source run history;
   restart recovery and cross-process worker leases with a global two-job limit.
   Cancellation retains the prior incremental checkpoint; interrupted runs can be
   retried without overlapping a live worker.
   **Implemented:** trusted explicit adapter-version migration chains, authenticated
   preview/apply controls with revision and plan checks, safe index reset defaults,
   and transactional configuration audit events retained after source removal.
   Audit records exclude values and secrets; verified sessions identify users,
   while shared server access is recorded without claiming a personal identity.
   **Implemented:** authenticated OAuth start and one-use external-browser handoff;
   expiring, encrypted attempt data in SQLite; exact callback/browser/state binding;
   S256 PKCE for Google/Spotify; per-connector Google read-only consent with no
   token fanout; attempt-specific status; redacted exchange/refresh errors and
   OAuth access-log queries; bounded state-checked native callback handling.
   **Implemented:** encrypted OAuth/PAT/API-key/IMAP bundles in the server vault;
   secret-free, connector-bound file references; on-read legacy migration plus a
   bounded one-time migration helper; refresh revision checks that reject stale
   writes after disconnect; fail-closed key loss and ciphertext validation.
   **Implemented:** named Notion page instances using protected, origin-bound
   bearer credentials; adapter-driven web configuration; isolated indexes and
   checkpoints; bounded search/block pagination and nested text reads; generic
   authenticated errors and no deletion inference from search omissions.
   **Implemented:** explicit web preview/apply import of legacy Notion tokens;
   expiring, identity-bound plans checked against current credentials and adapter
   version; recoverable reserved identities; import audit events; original
   connection retained and no automatic fetch or index reuse.
   **Implemented:** named account adapters for Gmail, Drive, Calendar, Contacts,
   Tasks, Spotify, Strava, Slack, Dropbox, Granola, Oura, GitHub Notifications and
   Weather. Each instance owns an encrypted bundle, index and checkpoint; no
   legacy-account fallback. Web token/application configuration, per-instance
   OAuth consent/status/refresh/disconnect reuse the completed shared security
   layer. Source revision checks and lifecycle locks reject stale changes;
   replacing authorization clears prior account evidence before accepting new
   credentials. Google consent stays scoped to the selected service.
   **Implemented:** explicit legacy imports for all named account adapters;
   complete encrypted OAuth bundles and token/API-key credentials; Weather
   location normalization; Google product/shared-file selection and GitHub
   filename compatibility. Bound previews reject credential/selection changes;
   stable reserved vault bindings recover interrupted imports without overwriting
   changed credentials. OAuth reviews disclose preserved grants and refresh-token
   sharing; imports neither contact providers nor reuse legacy indexes.
   **Implemented:** strict named Spotify/Strava scan contracts: staged bounded
   pagination, configurable page/document/deadline limits, origin-bound bounded
   HTTP, malformed/repeated-page rejection and cancellation without advancing
   successful checkpoints. Spotify labels provider-available recent history;
   Strava rereads accessible historical activities to capture edits and late
   uploads. Missing records never imply deletion.
   **Implemented:** strict named Slack scans: bounded, staged pagination of the
   user directory, conversations, history and thread replies; archived channels
   included; accessible history reread to capture older edits. Failed channels,
   malformed/unfinished pagination, rate limits and cancellation fail the entire
   scan without advancing its successful checkpoint. Web-configured limits,
   retention/coverage metadata and stable per-message identities; no deletion
   inference from missing or permission-filtered messages.
   **Implemented:** bounded SQLite WAL initialization retries for concurrent
   source-list and sync startup, with real reader-lock/concurrent-open tests.
   **Implemented:** strict named Gmail, Drive, Calendar, Contacts, Tasks,
   Dropbox, Granola, Oura, GitHub Notifications and Weather readers. Shared
   bounded/pinned HTTP, staged validation, per-instance web limits and safe limit
   errors; full accessible inventories or explicit windows reread for older
   edits; all nested collections paginated. Content-read failures abort rather
   than downgrade evidence. Scoped calendar/task IDs, per-record Oura identities,
   revision-checked Dropbox downloads, paginated Granola transcripts and complete
   weather response validation. OpenWeather query keys are injected only into
   the pinned wire request, with secret-free stored URLs and no redirects.
   Version-2 upgrade previews reset incompatible indexes/checkpoints while
   preserving vault bindings. These scans do not infer deletion or claim atomic
   snapshots; metadata-only/retention/window coverage remains explicit.
   **Implemented:** named password-based IMAP mailbox instances with web-configured
   TLS/STARTTLS endpoints, encrypted username/password bundles, revision-bound
   authorization changes and bounded staged read-only scans. DNS-pinned public
   destinations, verified TLS, UIDVALIDITY/UID identities, nonmutating body reads,
   MIME/byte/document/deadline safeguards and checkpoint-preserving failures.
   Coverage is one configured mailbox without attachments; no deletion inference.
   **Implemented:** explicit web preview/apply imports for legacy generic and
   Gmail IMAP connections; saved endpoint/security/port normalization, INBOX and
   bounded scan defaults; encrypted username/password copies without secret
   exports, provider calls or old-index reuse. Existing expiring caller-bound
   plans, credential-change checks, reserved identity recovery and import audit
   apply to both recipes. Original connections remain intact; login/access are
   verified only during an explicit sync.
   **Implemented:** per-instance opt-in LAN IMAP access with exact RFC1918/IPv6
   unique-local address authorization for the configured host/port. All DNS
   results must match; sockets remain pinned; localhost, link-local, metadata,
   public and unlisted addresses fail closed in LAN mode. Optional per-source
   PEM CA trust retains certificate/hostname verification without global trust
   changes. Database/web-managed settings, bounded address/certificate inputs
   and explicit v1→v2 public-default upgrade preserve existing indexes and
   credentials. Policy/endpoint edits use the existing revision/reset workflow.
   Legacy imports continue to default to public/system trust; other private
   network adapters remain future integrations. Live LAN mail-server acceptance
   remains pending; offline TLS tests verify private CA and hostname enforcement.
   OAuth security and encrypted credential storage are complete foundations;
   new providers reuse them. Never return credentials in source APIs.
4. **Implemented:** bounded Playwright `web_render` and strengthened Scrapy
   `web_crawl` through the existing governed ToolExecutor. Exact-origin pinned
   GET transport, strict robots policy, bounded workers/requests/bytes/output,
   isolated sandboxed browser contexts and standardized page provenance.
   Scrapy's default network handlers are disabled; partial/failed traversals
   return no evidence. Deep Research discovers configured public evidence tools
   from ToolSpec declarations and uses the active approval/capability executor;
   new providers require no provider-specific dispatch branch.
   **Pending acceptance:** install optional browser/crawler dependencies and
   validate Chromium sandbox/runtime behavior on the Ubuntu server, then desktop
   and Android end-to-end research/configuration checks. Synthetic offline tests
   do not substitute for those runtime checks.

## Phase 2: execution trace identity

**Implemented:** server-generated request/turn/trace IDs for HTTP requests,
including chat and Deep Research, with authenticated user and ownership-checked
session/conversation identity. The persistent session ID is the canonical
conversation ID. Response headers expose diagnostic IDs through CORS; research
SSE frames, session messages, agent/direct traces and research summary traces
carry the same correlation metadata. Shared API-key research without a verified
human session does not claim a personal identity. Client identity headers are
never authoritative.

Execution context follows bounded tool workers and asynchronous thread bridges;
collectors and agent SSE bridges filter shared-bus events by trace identity.
Unscoped and other-request events cannot enter a correlated trace. Failed agent
runs and research workers retain correlated failure traces with exception types,
without storing raw exception text. Trace persistence remains opt-in through the
existing trace configuration.

WebSocket chat now creates a fresh server identity for each received message,
including validation errors, and binds only the handshake-verified human user.
Chunk/done/error frames and completed/failed engine traces share that identity;
thread fallback inherits it and cancellation restores the ambient context.
Client identity fields are ignored. An optional `session_id` now selects an
existing, ownership-checked conversation for this engine-only endpoint. Human
login is required, credentials are rechecked on every message, and stored
history is authoritative across HTTP, WebSocket reconnects and other clients.
New user messages and completed answers retain the same verified correlation;
persistence failures produce an error rather than a false completion. Without
`session_id`, the turn stays stateless. Agent event
subscriptions forward the emitter's correlation separately from event data;
subscribing does not create an execution identity for the emitter.

Channel ingress now starts a fresh turn bound to a server-generated, persisted
channel-session ID. Existing channel history/preferences survive migration;
message metadata, execution events and system traces share the turn identity.
Channel sender IDs and webhook metadata do not establish an application user.
Deep Research channel bridges persist grounded response summaries and sanitized
failure summaries when tracing is enabled.

Scheduled tasks/operators create a fresh identity per run, without inheriting
creator/request identity from task metadata. Start/end events, agent traces and
persisted run logs carry it. Existing run logs migrate with empty correlation.
Manual operator runs inherit the current trusted execution scope or create a
standalone identity, with cleanup on success and failure.

Managed autonomous-agent ticks now create fresh execution identities covering
lifecycle events, retries, tool workers, stored traces and response messages.
Tool/activity listeners require both the managed agent ID and tick trace ID,
excluding delayed prior-tick events and unscoped events. Existing managed
messages migrate with empty correlation; background ticks do not claim a human
user or browser session from the initiating request.

**Implemented:** `scripts/check_session_continuity.py` provides a live service
acceptance workflow for HTTP → WebSocket → reconnect, saved history, matching
trace IDs, missing-session rejection and logout revocation. It uses a disposable
administrator login for trace inspection, retains one labeled test conversation
for device checks, and reports
failures without printing credentials or answer text. The workflow itself is
covered by offline integration tests; this is not evidence of a live-server pass.
See [Phase 2 acceptance](phase-2-acceptance.md) for the server command and device
checks.

**Pending:** executing the acceptance workflow on the Ubuntu service and checking
browser/desktop/Android behavior. Channel sessions remain separate
from ownership-checked browser/desktop sessions; identity diagnostics alone do
not complete the full trace-identity objective.

Phase 2 remains open until the evidence hard gate, runtime tool addition, trace
identity, knowledge maintenance, and client validation meet their acceptance checks.
The source-management foundation alone does not complete Phase 2.

---

# Upstream reference roadmap

## Current Focus Areas

These are the areas where active development is happening and contributions are most impactful:

- **Post-training data** — building datasets and training pipelines from execution traces to improve agent routing and tool selection
- **Multi-model orchestration pipelines** — coordinating multiple models within a single query (e.g., small model for classification, large model for generation)
- **Energy-aware routing** — using power consumption data from telemetry to optimize for energy efficiency alongside latency and quality
- **Plugin ecosystem** — community-contributed engines, tools, and agents distributed as Python packages
- **Federated memory** — memory backends that synchronize across devices
- **LLM-guided spec search:** Frontier-driven harness learning — a frontier model analyzes your traces and proposes config improvements. See [user guide](../user-guide/llm-guided-spec-search.md) and [architecture](../architecture/learning.md#llm-guided-spec-search-frontier-driven-harness-learning).

---

## How to Get Involved

1. Browse the workstreams below for an item that interests you
2. Check if a [GitHub issue](https://github.com/open-jarvis/OpenJarvis/issues) already exists for it — if not, [open one](https://github.com/open-jarvis/OpenJarvis/issues/new/choose)
3. Comment **"take"** on the issue to get auto-assigned
4. Read the [Contributing Guide](https://github.com/open-jarvis/OpenJarvis/blob/main/CONTRIBUTING.md) for development setup and PR process

---

## Workstreams

OpenJarvis development is organized into **five independent workstreams**. Contributors can pick any track that matches their skills and interests — workstreams are designed to be worked on in parallel without blocking each other.

Every item carries a maturity tag:

| Tag | Meaning | Contributor guidance |
|-----|---------|---------------------|
| **Ready** | Well-scoped, implementation path is clear | Pick it up — check [issues](https://github.com/open-jarvis/OpenJarvis/issues) for a spec or write one |
| **Design Needed** | Concept is clear but needs a spec before code | Start a [design discussion](https://github.com/open-jarvis/OpenJarvis/discussions) or draft an RFC |
| **Research-Stage** | Exploratory, needs investigation before designing | Read the relevant papers, prototype, share findings |

---

### Workstream 1: Continuous Operators & Agents

Operators are OpenJarvis's key differentiator — persistent, scheduled, stateful agents that run autonomously on personal devices. The current tick-based architecture (OperatorManager → TaskScheduler → AgentExecutor → OperativeAgent) is solid but needs hardening for truly long-horizon autonomy.

#### Where you can help

| Item | Maturity | Details |
|------|----------|---------|
| Operator health checks & heartbeat monitoring | **Ready** | Add liveness probes to OperatorManager; surface in `jarvis operators status`. Detect stalled operators beyond the existing reconciliation loop. |
| Metrics collection for operator manifests | **Ready** | The `metrics` field exists in `OperatorManifest` but is not collected. Wire it to telemetry. **Good first issue.** |
| Capability policy enforcement | **Ready** | `required_capabilities` field exists in manifests but is not enforced. Connect to the existing RBAC `CapabilityPolicy` system. **Good first issue.** |
| Rate limiting per operator | **Ready** | Prevent runaway operators from hammering inference. Add configurable rate limits to OperatorManager. |
| Operator composition / chaining | **Design Needed** | Express dependencies between operators (operator A feeds results to operator B). Requires design for data passing and scheduling semantics. |
| Event-driven operators | **Design Needed** | Operators that trigger on EventBus events (e.g., new file indexed, channel message received) rather than only cron/interval schedules. |
| Operator versioning & rollback | **Design Needed** | Run v2 of an operator alongside v1. Roll back automatically on repeated failures. |
| Self-improving operators via Learning | **Research-Stage** | Operators that use trace feedback to tune their own prompts, tool selection, and routing policies through the Learning primitive. |

---

### Workstream 2: Mobile & Messaging Clients

Personal AI must be accessible from the devices people actually carry. OpenJarvis runs on laptops, workstations, and servers — users interact via their phones.

**Currently supported:**

- **iMessage + SMS** via SendBlue — bidirectional, auto-detects iMessage vs SMS, thread replies, progress updates
- **Slack** via Socket Mode (slack-bolt) — bidirectional DMs, thread replies, Slack formatting, progress updates
- **Desktop/Browser** — Interact tab with real-time streaming, tool progress, telemetry footer

#### Where you can help

| Item | Maturity | Details |
|------|----------|---------|
| WhatsApp via Meta Cloud API | **Design Needed** | Baileys protocol is blocked by WhatsApp (405 errors). Need to implement via the official Meta WhatsApp Business API. Requires Meta Business account registration. |
| WhatsApp via Baileys (workaround) | **Blocked** | WhatsApp is actively blocking unofficial Baileys connections (405 Method Not Allowed). Monitor the [Baileys repo](https://github.com/WhiskeySockets/Baileys) for protocol updates. |
| Slack rich messages (Block Kit) | **Ready** | Current Slack responses use mrkdwn formatting. Add Block Kit support for structured responses with buttons, sections, and attachments. **Good first issue.** |
| Unified notification system | **Design Needed** | Push notifications when operators complete tasks or need user attention. Requires per-channel notification adapters. |
| Signal bidirectional | **Design Needed** | Currently send-only via signal-cli REST API. Add incoming message listener with background polling. |
| Voice interface | **Research-Stage** | Speech-to-text (Whisper) → agent → text-to-speech loop over phone channels. Existing `speech/` module provides a foundation. |
| Auto-restore channels on restart | **Ready** | Slack daemon and SendBlue auto-restore from saved bindings on server restart. Need to make this more robust for edge cases. |

---

### Workstream 3: Secure Cloud Collaboration

Personal AI's core tension: local models preserve privacy but lack capability; cloud models are powerful but require trusting a provider with your data. This workstream resolves that through **Minions-style collaborative inference** (local handles context, cloud handles reasoning) and **TEE-based confidential computing** (cloud cannot see your data even during inference).

**References:**

- [Minions: Cost-Efficient Local-Cloud LLM Collaboration](https://github.com/HazyResearch/minions)
- [TEE for Confidential AI Inference](https://openreview.net/forum?id=ey87M5iKcX) ([PDF](https://openreview.net/pdf?id=ey87M5iKcX))

#### Where you can help

| Item | Maturity | Details |
|------|----------|---------|
| Query complexity analyzer | **Ready** | Classify incoming queries by difficulty to decide local vs. cloud routing. Extends the existing `MultiEngine` routing logic. |
| Cost tracking per-query | **Ready** | `CloudEngine` already has pricing data. Surface per-query cost in traces and telemetry dashboards. **Good first issue.** |
| Redaction-before-cloud pipeline | **Ready** | Wire the existing `GuardrailsEngine` in REDACT mode as a mandatory pre-step before any cloud transmission. |
| Minion protocol (sequential) | **Design Needed** | Local model extracts and summarizes long context → cloud model reasons over the compressed result. Native reimplementation of the core [Minions](https://github.com/HazyResearch/minions) idea. |
| Minion protocol (parallel) | **Design Needed** | Local and cloud models work simultaneously on different aspects of a query; results are merged. Requires a new `HybridInferenceEngine` abstraction. |
| TEE attestation verification | **Design Needed** | Verify that cloud inference ran inside a trusted execution environment via cryptographic attestation. |
| Taint tracking across local/cloud boundary | **Design Needed** | The `TaintSet` already tracks PII/Secret labels. Add routing enforcement so tainted data only routes to attested TEE endpoints. |
| Speculative decoding (local draft + cloud verify) | **Research-Stage** | Local model generates candidate tokens; cloud model validates in parallel for latency reduction. |

---

### Workstream 4: Tutorials & Documentation

OpenJarvis has reference docs and four tutorials, but critical gaps remain in continuous agents, LM evaluation, learning approaches, and custom tools. Video tutorials are scoped as a contributor opportunity — written tutorials come first, with video scripts included so anyone can record.

#### Where you can help

| Item | Maturity | Details |
|------|----------|---------|
| "Building Continuous Agents" tutorial | **Ready** | Writing an operator TOML manifest, activating it, session persistence across ticks, daemon mode. Example: a research operator that monitors arxiv daily. |
| "Adding Custom Tools" tutorial | **Ready** | Implementing `BaseTool`, registering via `ToolRegistry`, wiring into agents. Example: a weather API tool. **Good first issue.** |
| "Testing & Comparing LMs" tutorial | **Ready** | Running benchmarks, comparing local vs. cloud models, interpreting telemetry (latency, cost, energy per token). Uses the existing `bench/` framework. |
| Per-platform installation guides | **Ready** | Expand `installation.md` with platform-specific walkthroughs: macOS + Ollama, Ubuntu + NVIDIA + vLLM, Windows + Ollama, Raspberry Pi. **Good first issue.** |
| "Learning & Model Selection" tutorial | **Design Needed** | Router policies (heuristic, learned, GRPO), proposed approaches like Thompson Sampling, trace-based reward signals. |
| Video tutorial infrastructure | **Design Needed** | Establish recording workflow, hosting (YouTube), MkDocs embedding. Write video scripts alongside written tutorials. |
| Interactive Jupyter notebook tutorials | **Design Needed** | Notebook versions of key tutorials for exploratory, cell-by-cell learning. |

---

### Workstream 5: Hardware Breadth

Personal AI means running on the hardware people actually own. Each new hardware target expands who can use OpenJarvis and generates data for the research agenda (energy, cost, latency tradeoffs across silicon).

Adding a new hardware target involves up to four components: hardware detection in `core/config.py`, an inference engine adapter in `engine/`, an energy monitor in `telemetry/`, and an entry in the GPU specs database in `telemetry/gpu_monitor.py`.

#### Where you can help

| Item | Maturity | Details |
|------|----------|---------|
| AMD Ryzen AI iGPU path | **Ready** | Strix Point RDNA 3.5 iGPU handles 7-8B via Vulkan. llama.cpp Vulkan backend works today. Needs hardware detection and energy monitor. **Good first issue.** |
| GPU specs database expansion | **Ready** | Add Intel Arc, Jetson Orin, Snapdragon specs to `GPU_SPECS` in `telemetry/gpu_monitor.py` (TFLOPS, bandwidth, TDP). **Good first issue.** |
| Intel Arc GPU (B580/B570) | **Design Needed** | 12GB VRAM, ~$250 consumer GPU. Viable for 7-8B models. Engine path: IPEX-LLM or llama.cpp SYCL backend. |
| NVIDIA Jetson Orin | **Design Needed** | Best-in-class edge device. Orin NX 16GB handles 7-8B models at 15-25 tok/s. Needs hardware detection, energy monitor (tegrastats), deployment guide. |
| Qualcomm Snapdragon X Elite NPU | **Design Needed** | 45 TOPS, Windows Arm laptops. ONNX Runtime + QNN Execution Provider is the viable path. |
| Intel Lunar Lake NPU via OpenVINO | **Design Needed** | 48 TOPS — most mature NPU software stack for x86 laptops. New engine wrapping OpenVINO GenAI. |
| Raspberry Pi 5 | **Design Needed** | CPU-only via llama.cpp ARM NEON for 1-3B models. $100 entry point for hobbyists. |
| Unified hardware benchmark suite | **Design Needed** | Standardized benchmark that runs the same workloads across all supported hardware, producing comparable energy/latency/throughput/cost numbers. |
