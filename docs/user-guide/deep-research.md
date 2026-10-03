# Deep Research

A multi-hop research agent that searches across your indexed documents, cross-references information, and returns answers with citations. It reasons through complex queries step by step, pulling context from multiple sources in your local knowledge base.

## Quickstart (5 minutes)

### 1. Install and initialize

```bash
git clone https://github.com/open-jarvis/OpenJarvis.git
cd OpenJarvis
uv sync --extra dev
jarvis init --preset deep-research
```

This writes a pre-configured `~/.openjarvis/config.toml` for the deep research agent.

### 2. Index your documents

```bash
# Install Ollama: https://ollama.com
ollama pull qwen3.5:9b

# Index a directory of files
jarvis memory index ./docs/
jarvis memory index ~/Documents/papers/
```

OpenJarvis chunks the content and stores it in a local SQLite/FTS5 database. Supported formats include `.txt`, `.md`, `.pdf`, `.py`, `.json`, `.csv`, and more.

### 3. Ask a research question

```bash
jarvis ask "Summarize all documents about transformer architectures"
```

The deep research agent will:

1. Search your indexed documents for relevant chunks
2. Reason across multiple sources (up to 8 hops)
3. Synthesize a coherent answer with references to source documents

## CLI Commands

```bash
# Ask a question (uses deep_research agent by default with this config)
jarvis ask "What meetings did I have with Alice last month?"

# Explicitly specify the agent
jarvis ask --agent deep_research "Compare the approaches described in paper-a.pdf and paper-b.pdf"

# Index more documents
jarvis memory index ~/Downloads/reports/
jarvis memory index ./notes.md

# Search memory directly
jarvis memory search "project timeline"
jarvis memory search -k 20 "budget estimates"

# Check what's indexed
jarvis memory stats
```

## Configuration Reference

The preset writes this to `~/.openjarvis/config.toml`:

```toml
[engine]
default = "ollama"

[intelligence]
default_model = "qwen3.5:9b"
temperature = 0.3               # Low temperature for factual research

[agent]
default_agent = "deep_research"
max_turns = 8                   # Multi-hop reasoning steps

[tools]
enabled = ["knowledge_search", "knowledge_sql", "scan_chunks", "think", "web_search"]

[tools.storage]
default_backend = "sqlite"
```

### Key settings

| Setting | Default | Description |
|---------|---------|-------------|
| `intelligence.default_model` | `qwen3.5:9b` | The model used for reasoning. Larger models (e.g., `qwen3.5:35b`) give better results on complex queries. |
| `intelligence.temperature` | `0.3` | Low temperature keeps answers factual. Increase for more creative synthesis. |
| `agent.max_turns` | `8` | Maximum reasoning hops. Increase for deeply nested research tasks. |
| `tools.enabled` | 5 tools | `knowledge_search` (semantic), `knowledge_sql` (structured), `scan_chunks` (browse), `think` (reasoning scratchpad), `web_search` (online fallback). |
| `tools.storage.default_backend` | `sqlite` | FTS5-backed full-text search. Also supports `faiss`, `colbert`, `bm25`, and `hybrid`. |

## Example Queries

```bash
# Summarize across multiple documents
jarvis ask "Summarize all emails about the Q3 budget review"

# Cross-reference sources
jarvis ask "What do papers A and B agree on regarding attention mechanisms?"

# Find specific information
jarvis ask "What meetings did I have with Alice last month?"

# Extract structured data
jarvis ask "List all action items from the meeting notes in ~/Documents/meetings/"

# Research with web fallback
jarvis ask "Compare our internal benchmarks with the latest published results"
```

## Indexing Different Data Sources

### Local files and directories

```bash
# Recursively index a directory
jarvis memory index ~/Documents/

# Single file
jarvis memory index ./report.pdf

# Custom chunk size for long documents
jarvis memory index ./paper.pdf --chunk-size 1024 --chunk-overlap 128
```

### PDFs

PDFs are automatically extracted and chunked. For best results with scanned PDFs, ensure they have been OCR-processed.

```bash
jarvis memory index ~/Papers/*.pdf
```

### Web pages

Use the `web_search` tool (enabled by default in this config) to pull in online sources at query time. For persistent indexing of web content, download pages first:

```bash
curl -s https://example.com/article | jarvis memory index --stdin --source "example.com"
```

### Code repositories

```bash
jarvis memory index ./src/ --chunk-size 256
```

Smaller chunk sizes work better for code, where each function or class is a natural unit.

## Troubleshooting

**"No results found"** -- Make sure you have indexed documents first with `jarvis memory index`. Check indexed content with `jarvis memory stats`.

**Answers are too vague** -- Try increasing `max_turns` in the config (e.g., `12` or `15`) to give the agent more reasoning steps. You can also try a larger model like `qwen3.5:35b`.

**Slow responses** -- The agent makes multiple search passes. Each turn involves a model call. Reduce `max_turns` or use a smaller model (`qwen3.5:4b`) for faster but less thorough results.

**Web search not working** -- The `web_search` tool requires the Tavily API. Install with `uv sync --extra tools-search` and set `TAVILY_API_KEY`.

**Wrong chunks retrieved** -- Try re-indexing with different chunk sizes. For technical documents, smaller chunks (`256`) often retrieve more precisely. For narrative text, larger chunks (`1024`) preserve more context.

## Configured sources

In **Data Sources → Configured sources**, choose **Add source**, select an adapter,
name the connection, and fill in its fields. **Test connection** validates the
configuration without saving it. **Save source** persists it; **Sync** indexes it.
Multiple Local Files connections can point at different folders (or the same
folder) with independent indexing, checkpoints, and lifecycle controls.

Local Files accepts folders on the OpenJarvis server such as `/mnt/ai/documents`.
Supported formats are UTF-8 text, Markdown, CSV, TSV, JSON, HTML and text-based
PDFs. PDF support requires the `memory-pdf` extra; scanned PDFs require OCR.
Hidden files/directories and symlinks are excluded. Limits are 8 MiB per file,
200 pages per PDF and 2,000 supported files per scan; exceeding a limit reports
an error rather than silently truncating the source.

Configuration lives in `sources.db` under the OpenJarvis configuration directory.
The initial `connectors/local_files.json` connection is imported automatically,
with a `.json.migrated` backup and its existing document identities retained.
There is no need to disconnect/reconnect to migrate. Other integrations continue
using their existing connection controls until their adapters are migrated.

Sync refreshes changed files and removes missing files from the index only after
a complete successful scan. Failed scans retain documents not yet reached.
Changing a folder clears only that connection's index and checkpoint; sync again
after saving. **Remove** clears its indexed documents and configuration, keeping
original files. **Disable** prevents further sync/tool reads but retains indexed
knowledge; remove the connection if its indexed data should no longer be recalled.
Edits/removal are rejected while a sync owns the connection. Sources survive
restarts; an interrupted sync is reported and can be retried.

Tool reads still require `connector:local_files:read` and `file:read`. Configuring
a source grants no tool permissions. Adapter discovery supplies versioned fields
and operation metadata for the frontend; unsupported config versions are rejected.
These adapters contain no secret fields. Credentials use protected references.

### Scheduled sync, progress and cancellation

Each source has an opt-in schedule, disabled initially. Enable it in the source
manager, choose an interval from 5 minutes to 7 days, and save. The first run is
due one interval after saving. Due times use UTC; these are elapsed intervals,
not calendar or cron schedules. A disabled source keeps its schedule settings
but does not run. The server must be running for schedules to execute.

Schedules and jobs persist in `sources.db`. Missed intervals coalesce into one
run, rather than a backlog. Busy sources and the global limit of two background
sync workers defer due work to a later scheduler tick. Worker leases prevent
another server process from starting the same source. Queued jobs resume after
a restart, unless their source configuration changed or the source was disabled.
An abandoned running job is marked **Interrupted**; retry with **Sync**, or wait
for the next scheduled run. Its previous successful token and watermark remain.

The latest run shows its phase, documents read, pages fetched and chunks written.
These counters describe work performed, not necessarily new documents. **Run
history** loads the latest 20 runs; at most 100 completed runs are retained per
source. Removing a source removes its schedules and run history. This operational
history is separate from the configuration history described below.
Background API syncs create run records; synchronous command-line/tool reads do not.

**Cancel** requests a cooperative stop between files, pages and response reads.
A blocked network read or document parser must reach its next boundary first.
Already indexed chunks can remain, but cancellation preserves the previous
incremental token and skips final missing-document/deletion reconciliation.
Once a job enters its final commit phase, cancellation is rejected so cleanup and
checkpoint advancement finish consistently. Shutdown requests cancellation of
local workers and waits up to five seconds after stopping the scheduler; blocked
work can outlast that grace period and retains its leases until it stops.

Schedule edits use their own revision and do not clear indexed knowledge or
change the source configuration revision. Schedule, history and cancellation
endpoints require the same authenticated access as the source manager. Run
records store IDs, timestamps, counters and bounded errors, never credentials
or fetched response bodies.

### Configuration upgrades and audit history

When trusted server adapter code introduces a new configuration version, existing
sources retain their saved settings. They cannot sync until upgraded, and their
scheduled syncs wait without generating repeated failed jobs. The source manager
shows an available upgrade only when every version has an explicit migration step.
Unknown adapters, future versions and incomplete migration paths stay blocked;
install compatible trusted adapter code to resolve them. No adapter code is
downloaded or installed by this workflow. Existing built-in adapters remain at
version 1; this mechanism supports their future changes without forcing an upgrade.

Choose **Preview configuration upgrade** to see the proposed settings and whether
the index and checkpoint will be retained or cleared. Preview performs local
transformation and validation, without fetching remote content, decrypting
credentials, changing settings or writing an audit event. Choose **Apply
configuration upgrade** to save that preview. The server rechecks both the source
revision and the proposed plan; if either changed, preview again. Busy sources
cannot be upgraded. Scheduling settings remain intact, and queued jobs created
against an older source revision are cancelled before fetching.

Adapter migrations default to clearing that source's indexed documents and
checkpoint. Trusted adapter authors may explicitly preserve them only when the
transformation preserves document identity, ingestion and incremental-token
semantics. Imported Local Files sources always reset on their first adapter upgrade
to replace path-dependent legacy IDs with source-instance IDs. Other sources are
unaffected. After a reset, sync again to rebuild knowledge. Index cleanup uses the
existing checkpoint-first reset procedure; an interrupted or failed cleanup can
require reindexing even if the configuration upgrade has not committed.

Migration steps are registered in server code as `ConfigMigration(from_version,
transform, preserves_index=False)` entries on `SourceAdapter.migrations`. Each
pure, deterministic transform advances exactly one version, N to N+1. The final
result must pass the adapter's current validation and retain the same protected
credential reference and valid origin binding. Missing steps, validation failures
or changed credential references reject the upgrade. Adapter discovery and
configuration upgrades grant no tool capabilities.

**Configuration history** records successful creation/import, edits, schedule
edits, upgrades and removal. **All configuration history (including removed
sources)** also exposes records for deleted sources. It loads 50 events at a time
and supports loading older changes. Events persist across restarts, are retained
without automatic pruning, and include source/adapter IDs, action, timestamp,
revision, version, changed field names and whether indexing was reset. Setting
changes and their audit events commit in the same source-database transaction.
Existing databases receive the audit table without invented historical events.

Verified login sessions record `user:<user_id>`; shared API-key/server access
records `server_access`, and internal operations record `system`. The identity is
derived on the server, never from a supplied actor field. Audit records exclude
names, configuration values, URLs, response bodies, credential IDs and secrets.
They describe committed changes, not rejected attempts, and do not provide undo,
credential-vault history or tamper-proof logging. All audit and migration endpoints
require the same authenticated access as other source-management endpoints.
You can disable or remove an unsupported source while keeping its saved settings.

### Governed public crawling and rendered pages

`web_crawl` uses Scrapy to read a bounded subset of a supplied website;
`web_render` uses Playwright to read one page with JavaScript. These are optional
research tools, separate from persistent source instances and the legacy
interactive browser tools. They neither import account cookies nor authorize
logins, form submissions, file downloads or arbitrary browser actions.

Install the optional packages on the existing Ubuntu server from the source tree:

```bash
cd ~/.openjarvis/src
uv sync --inexact --extra browser --extra tools-crawl
uv run --no-sync playwright install chromium
```

Playwright requires version 1.48 or later and a working Chromium sandbox. Install
Chromium's Linux system dependencies if its installer reports them missing; do
not disable the sandbox to work around an unsupported runtime. After configuration,
restart the existing `openjarvis-api.service` rather than starting a second API.

Add `web_crawl` and `web_render` to your existing `[tools].enabled` list when you
want them available. Preserve the other configured tools. Enabling/discovering a
tool never grants authorization: `web_crawl` requires `network:fetch`, and
`web_render` also requires `code:execute` because it executes site JavaScript.
Existing tool approval, enable/disable policy, capability grants, boundaries and
trace checks still apply. Browser Deep Research advertises configured tools whose
ToolSpec declares current/external evidence; it dispatches them through the active
agent's guard and executor and validates the returned evidence before synthesis.
Additional public evidence tools can use the same declaration and result contract.

Both tools accept public HTTP(S) URLs on ports 80/443 without embedded credentials
or secret query parameters. Every fetch resolves/validates public IPs and pins its
connection; redirects and linked resources stay on the exact scheme/host/port.
Robots policy is fetched through that same transport. A missing robots file (404)
allows access; denials, unavailable policies and robots redirects fail closed.
Neither engine uses its own HTTP downloader, cookies or authenticated sessions.

Crawls default to three pages (maximum eight), with link depth at most two. The
crawler disables its normal HTTP handlers, retries and cookies; failed selected
pages abort the staged result. Rendering creates a fresh context with service
workers, WebSockets, non-GET requests, popups and off-origin resources blocked.
Chromium fallback traffic uses an unavailable local proxy, and native WebRTC/
WebTransport APIs are disabled. Sites that need external CDN scripts, cookies or
live socket feeds may provide only a subset or fail; rendering does not claim a
complete application snapshot. Browser extraction observes the DOM after load.

Each tool shares a budget of 50 HTTP requests, 2 MiB per response and 16 MiB total;
robots/resources count too. Crawling has a 60-second worker limit; rendering has a
35-second worker limit. At most two workers run concurrently per API process;
a busy worker pool returns an explicit retry error. Worker process groups are
closed after completion or timeout, and output is capped at 256 KiB. Page text is
limited to 4,000 characters, with truncation disclosed; titles are capped at 300.
Successful results carry actual page URLs, fetch/observation times, source IDs and
bounded-coverage metadata. Failed/malformed results return no page evidence.
Source content remains untrusted data and never supplies instructions or grants.

Optional-library/Chromium behavior and desktop/Android research still require
runtime acceptance on the deployed server; the regression tests use synthetic
transports and library seams without outbound networking.

### Web Page and JSON API sources

**Web Page** indexes one HTML, plain-text or Markdown URL. It extracts
readable HTML text and a page title, excludes script/style/template and explicitly
hidden elements, and does not execute JavaScript or follow page links. Add one
named connection per page. For browser-rendered sites and crawling, the existing
Playwright/Scrapy tools remain separate governed operations (see below).

**JSON API** fetches a JSON endpoint with GET. The default **Whole JSON
document** mode indexes the response without guessing field meanings. Choose
**Individual records** to map an array to documents:

| Setting | Example | Meaning |
|---|---|---|
| Records array pointer | `/data/items` | Array to index; empty means the root array |
| Record ID pointer | `/id` | Nonempty string or integer that stays stable across updates |
| Title pointer | `/title` | Optional string/number title; empty uses the record ID |
| Content pointer | `/body` | Text/value to index; empty indexes the entire record |
| Maximum records | `200` | Error if the array exceeds this count; maximum 1,000 |
| Complete snapshot | Off by default | Enable only if the response contains the whole collection; then missing records are removed after a successful sync |

Pointers start with `/`; use `~1` for a slash in a key and `~0` for a tilde.
Duplicate IDs, missing pointers, invalid JSON, duplicate object keys and non-finite
numbers report errors before any new records are indexed. Without **Complete
snapshot**, records absent from one response remain indexed. Pagination and incremental sync are configured explicitly below.

**Test connection** actually fetches and parses either source without
saving/indexing it. Fetching accepts public HTTP/HTTPS addresses on ports 80/443,
pins verified DNS addresses while keeping TLS hostname verification, revalidates
redirects, and rejects private/metadata addresses and HTTPS-to-HTTP downgrades.
There are at most five redirects, four address attempts per destination, a 2 MiB
response limit, 20-second I/O timeouts and a checked 60-second fetch budget.
Compressed, partial, failed and unsupported-format responses report errors.
Private services belong in explicitly scoped integrations rather than these
adapters. Unauthenticated connections send no credentials or cookies. With a
protected credential selected, only its configured authorization header is sent;
credential-bearing URLs and recognized credential query parameters are rejected.
Do not put secrets into a URL or source configuration.

### Protected credentials and authenticated connections

In **Protected credentials**, choose **Add credential**, name it, enter the HTTPS
origin (for example `https://api.example.com`), choose **Bearer token** or **API key
header**, and enter the secret. API keys require a header name such as `X-API-Key`;
reserved headers are rejected. Save it, then select the credential in the Web Page
or JSON API source form. **Test connection** uses that credential without indexing.
The source URL must match its origin; authenticated redirects must remain on the
same HTTPS origin. DNS pinning, private-address rejection and fetch limits still
apply. These named Web Page/JSON API adapters use bearer/API-key references;
they do not automatically refresh OAuth credentials or send cookies/Basic auth.
Account-specific OAuth controls are described below.

Credentials are encrypted with Fernet in `source_credentials.db`, alongside the
source database. Only metadata and reference IDs are returned by the APIs. Secrets
are entered in password fields and are never prefilled or persisted in browser
storage by OpenJarvis. Responses reflecting the active secret (including common
encodings) are rejected before indexing; authenticated parsing errors are generic
to keep response data out of status messages.

Keep `source_credentials.db` and its original `source_credentials.key` together in
protected server backups. Both are restricted to the server account (0600). The
key is separate from the database, but remains on the same server: anyone with
access to both can decrypt the credentials. Losing the key makes existing values
unreadable; OpenJarvis will not silently create a replacement key for existing
records. Use **Rotate** to replace a secret with a revision check. Rotation and
removal are rejected during active use; detach a credential from every source,
including disabled sources, before removing it. The origin and header binding are
immutable; create a new credential to change them.

Install/update the server dependencies with `uv sync --extra server` (or the
existing desktop extra) and restart `openjarvis-api.service`. Authenticated source
management uses the server API authentication already configured. Tool-driven reads
from credential-capable adapters additionally require `credential:use`, even for a
currently public instance, so a later configuration change cannot bypass that
grant. Network and connector grants remain required; configuring a source does not
grant tool access.

Indexing records the original URL, final URL, response hash, fetch time and source
instance identity. Search evidence retains the actual fetch timestamp, so a search
does not make cached web content appear newly fetched. Failed syncs retain the
prior successful watermark and do not reconcile missing documents.

Tool reads require the selected adapter's `connector:web_page:read` or
`connector:json_api:read` capability plus `network:fetch`. Saving a source grants
none of these capabilities. Existing Local Files permissions are unchanged.


### Paginated JSON collections

In **Individual records**, select **Next-page URL in JSON** or **Page cursor in
JSON** and provide the **Next-page value pointer**, such as `/next`. Every page
must contain that field. `null` or an empty string explicitly finishes the scan;
a missing field is an error. For cursor pagination, set the query parameter name
(default `cursor`). OpenJarvis preserves the source URL's other query parameters.
For URL pagination, the API's next URL is authoritative and may be relative to
that page's final URL. All page URLs and redirects must stay on the configured
origin, including scheme and effective port. Private-address and credential
checks apply to every page. Credentials remain bound to their HTTPS origin.

Set **Maximum pages** (default 10, maximum 50) and **Maximum records and deletion
IDs per sync** (default 200, maximum 1,000). Limits apply to the entire scan. Each
page is limited to 2 MiB, the total response data to 10 MiB, and all pages share a
checked 60-second fetch budget. Repeated URLs/cursors, duplicate record IDs across
pages, malformed pages, and an unfinished scan at a limit cause an error before
any page is yielded for indexing. Pages are staged in memory within these bounds;
mid-page resume, offset/page-number pagination and HTTP Link-header pagination
are not yet supported.

**Complete snapshot** removes absent records only after every page has finished
and all documents have been ingested. Enable it only if the API provides a stable,
complete collection throughout the scan. Concurrent changes between pages can
otherwise make a collection incomplete; pagination alone does not guarantee a
consistent snapshot. With this option off, missing records remain indexed.

### Incremental API changes and explicit deletions

Choose **Incremental changes with durable token** only for an API that defines
this contract. Its initial request without a token must provide the bootstrap
records you want indexed. Configure the **Final-page sync token pointer**, such
as `/sync_token`, and the **Sync token query parameter**, default `since`.
The final page must provide a nonempty string or integer token. On the next sync,
OpenJarvis sends that token to the same source URL. Tokens are opaque, non-secret
continuation state (maximum 2,048 UTF-8 bytes); do not use access tokens here.
Authentication belongs in protected credentials. Page cursors and durable sync
tokens use different query parameters. Pagination cursors are transient and are
never saved as the durable token.

Optionally configure **Deleted record IDs pointer**, such as `/deleted_ids`.
Each page must then provide an array of string/integer IDs, including an empty
array when nothing was deleted. IDs use the same typed identity as normal
records. A scan cannot both update and delete the same ID. Explicit deletions
remove only that connection's documents and FTS entries. Incremental mode always
retains records absent from the response and cannot use complete-snapshot cleanup.

The durable token advances only after all pages validate, ingestion succeeds and
cleanup finishes. Empty successful deltas can advance the token. A failed scan,
ingestion failure or cleanup failure retains the old token and successful
watermark; retrying requests the same change window. Individual document refreshes
are atomic, while a whole multi-document ingestion is not: an ingestion failure
may leave some refreshed documents visible, and replaying the unchanged token
safely retries them. Deletion batches are atomic. APIs must make token-based replay
safe and retain change history long enough for retries.

Source configuration changes reset its checkpoint and clear its indexed data;
changing the sync contract therefore bootstraps again. **Test connection** validates
the complete configured scan without saving its token or applying deletions.
Tool-driven reads do not commit tokens or modify the index; they make an initial
read and remain bounded by the same configuration and capability requirements.

### Account-specific OAuth connection security

OAuth here grants access to an external service; it is separate from your Jarvis
login and requires neither Active Directory nor centralized authentication.
Existing Google, Spotify and Strava account controls start authorization through
an authenticated `POST /v1/connectors/{id}/oauth/start`. The response contains an
opaque attempt ID and a launch path. The frontend opens a blank popup during the
user's click, then navigates it to that handoff. Jarvis API keys and session tokens
are sent only through authenticated API headers, never in the popup URL.

The one-use launch ticket expires after one minute. Launch sets an HttpOnly,
SameSite=Lax cookie in the actual external browser, then redirects to the provider.
The callback must match that browser, connector, provider, state and exact server
callback URI. State is consumed atomically before token exchange, so replay and
parallel callbacks cannot exchange twice. The callback itself needs no Jarvis API
header: the valid, browser-bound attempt authorizes it. A callback without that
attempt is rejected even if the caller sends a Jarvis API key. Disconnecting the
connector cancels pending attempts.

Launch/callback attempts are persisted in `oauth_attempts.db` with 0600 permissions;
sensitive payloads, including the PKCE verifier, are encrypted using the server's
`source_credentials.key`. Raw handoff tickets, callback state and browser secrets
are not stored. Unused handoffs expire after one minute; issued callbacks expire
after ten minutes. At most 100 active attempts are accepted, expired rows are
pruned on the next start, and recent terminal status rows are bounded. A restart
can preserve an unused or issued attempt when the database and key are intact.
A process interruption during an already consumed exchange requires a new attempt.
The frontend polls the authenticated status of its own attempt, rather than
mistaking an already-connected account for successful new consent.

Google and Spotify use S256 PKCE to bind authorization codes to a server-held
verifier. Spotify's PKCE exchange uses its public-client request format; Google
also sends its registered client credentials. Strava uses its documented client
secret flow with browser/state binding; no unsupported PKCE behavior is assumed.
Token exchanges and Google refresh calls reject redirects and disable environment
proxy inheritance. Response/token validation failures do not overwrite the
previous valid credentials. Token endpoint bodies, exceptions and provider denial
text are excluded from callback pages and refresh errors. Callback pages send
no-store, no-referrer and restrictive content-security headers.

A Google connection requests only the selected service's read-only scope:
Drive, Calendar, Contacts, Gmail or Tasks. Its resulting tokens are saved only to
that connector's file; consent does not silently connect the other Google products.
Client application registration can still be shared across those products.
Previously granted permissions are not remotely revoked by this update. New
read-only consent does not authorize email modification or calendar writes; any
future write integration needs an explicit additional consent design, as well as
Jarvis's existing tool capability/approval checks. Successful consent grants no
Jarvis tool capabilities. Named source instances for these legacy account
integrations remain the next migration step.

Register the exact `/v1/connectors/{id}/oauth/callback` URI with the provider. Server
callbacks require HTTPS, except for HTTP loopback addresses. An HTTP LAN hostname
or address is rejected. For Google server callbacks use a Web application OAuth
client; native loopback flows use a Desktop app client. Native callback listeners
bind before opening consent, validate state and exact path, suppress callback
request logs, and have a total two-minute deadline plus bounded socket reads.

OpenJarvis's standard Uvicorn access logger redacts OAuth launch/callback query
strings. Reverse proxies and other logging systems must likewise omit those
queries, since handoff tickets and authorization codes are short-lived secrets.
Connector access/refresh tokens, client secrets, personal access tokens, API keys
and IMAP credentials now use encrypted bundles in `source_credentials.db`, sharing
its original `source_credentials.key`. The old `connectors/*.json` credential
locations contain only versioned vault references, bound to their connector file.
Google's shared registration fallback still works. New connections and refreshes
write encrypted bundles; reading valid legacy credential JSON migrates it before
returning values. This includes Google, Spotify, Strava, Notion, Dropbox, Slack,
Granola, Gmail IMAP, Oura, GitHub Notifications and Weather. Local folder and RSS
configuration remains ordinary configuration.

Migration commits the encrypted bundle before atomically replacing the plaintext
file. If replacement fails, the original file remains available for retry; no
plaintext backup is created. Repeated migration is safe. Missing/incorrect keys,
modified ciphertext, swapped references and symbolic-link credential paths are
rejected. Disconnect removes the reference and encrypted row; a refresh that read
an older revision cannot restore disconnected credentials or overwrite a newer
refresh. Disconnect does not revoke provider grants or erase historical backups.
Existing plaintext backups should be protected or retired separately.

For an immediate migration of all known credential files, run this as the same
account that runs OpenJarvis, from the updated checkout, while the service is
stopped:

```bash
sudo systemctl stop openjarvis-api.service
uv sync --extra server
uv run python -c 'from openjarvis.core.config import DEFAULT_CONFIG_DIR; from openjarvis.connectors.token_vault import migrate_connector_tokens; print("Migrated credential files:", migrate_connector_tokens(DEFAULT_CONFIG_DIR / "connectors"))'
sudo systemctl start openjarvis-api.service
```

Do not change accounts with `sudo uv`: the config directory and key belong to the
server account. Back up the vault database, its original key, and the secret-free
connector reference files together. File bindings are relative to that directory,
so restoring the complete configuration under a new home preserves references.
Vault bundles are internal and do not appear in the metadata-only credential APIs
or browser storage. OAuth remains optional: local folders, public sources and
manually configured bearer/API-key source connections continue independently.


### Named Notion page connections

In **Data Sources → Configured sources**, add a protected **Bearer token**
credential with HTTPS origin `https://api.notion.com`. Use a Notion internal
integration token with permission to read the desired content, and share the
relevant pages with that integration in Notion. Then choose **Add source → Notion
pages**, give the connection a name, and select that credential. Only compatible
Notion bearer credentials appear in the selector; authentication is required.

Each connection can use a different integration/workspace and optional title
filter. **Test connection** makes one small read-only search request and does not
save or index anything. Save, then **Sync**, or enable an interval schedule. The
existing edit, enable/disable, job history, cancel, remove, and credential rotation
controls apply. Tokens stay in the vault; saved source configuration and APIs
contain only the credential ID and non-secret scan settings.

Page IDs are scoped to the named instance, so the same provider page can appear
in two connections without sharing indexes or checkpoints. Editing settings resets
only that instance's index. Removing a source removes its indexed documents while
retaining its reusable credential; detach all sources before deleting a credential.
The adapter declares Notion read, network fetch and credential-use capabilities;
configuring it does not grant an agent permission to read it.

Reads use a pinned page/block API contract (`Notion-Version: 2022-06-28`) and the
fixed `https://api.notion.com` origin. Search POST redirects are rejected; block
GETs retain the protected same-origin and public-address checks. Both search and
block lists paginate, and child blocks are traversed to a maximum nesting depth
of eight. Supported text blocks render as Markdown. Files, image contents and
linked database contents are not downloaded by this reader.

The default scan limits are 100 pages, 300 requests and 5,000 blocks; the form can
adjust them within server bounds. Responses are capped at 2 MiB each and 16 MiB
per scan, with a two-minute deadline. Cyclic/malformed pagination, inaccessible
content, cancellation and exceeded limits fail without advancing the checkpoint.
The reader validates the scan before yielding documents for indexing. Provider
modification time, actual fetch time, content version and source-instance identity
remain attached to the evidence.

Notion search is not a reliable complete workspace inventory. This adapter
therefore does not delete indexed pages merely because a later search omits them;
explicit source removal or configuration reset remains available for cleanup.
The old single-account Notion connector retains its separate identity and setup.
It is not automatically imported or disabled by creating a named instance; avoid
syncing both against the same pages if you want to avoid duplicate legacy evidence.
Named token and OAuth account connections are available as described below.
The Notion import flow can reuse an existing server token.


### Import an existing Notion connection

Under **Data Sources → Configured sources → Import existing connections**, choose
**Import Notion**, enter a name, and select **Preview import**. The preview shows
the named adapter's default scan settings, protected credential origin and import
effects. Select **Apply import** to create the named connection without entering
or displaying its token in the browser. The token is copied server-side to a new
origin-bound bearer credential in the encrypted vault.

Preview does not create a source, copy a credential, rewrite legacy JSON or
contact Notion. Applying the import also performs no provider request and does not
start syncing. A valid plaintext legacy credential file is upgraded to a vault
reference during apply. Existing vault references and the original connection are
kept. The named instance starts with its own empty index and checkpoint; existing
legacy documents are neither copied nor deleted. Choose **Sync** when ready.

Preview tickets expire after ten minutes and bind to the verified session identity
(or shared server-access context), adapter version, chosen name, settings and
current legacy credential snapshot. If the old token changes or is disconnected,
preview again. Tickets are sent in authenticated request bodies, never URL query
strings; source and audit responses contain no tokens or secret fingerprints.

Imports reserve persistent source and credential IDs before copying the token.
If the process stops after saving a credential but before committing the source,
retry with a valid preview to reuse those IDs. Source creation, the completed
import marker and its audit event commit together. A retry after completion returns
the existing source; it does not copy another credential or reset edited settings.
A pending credential that was independently rotated is never silently overwritten.

Each legacy Notion connection can be imported once. Removing the imported source
keeps a record of that import, so replaying an old request cannot recreate it. Use
**Add source** for a deliberate new connection. New named sources and legacy
connections have independent credentials; changing one token does not rotate the
other. After checking the named source, use the existing connection controls to
retire the old integration if you want to stop indexing the same pages twice.

Only trusted, server-defined import recipes are supported. Clients cannot choose
arbitrary credential paths or provider origins. Notion and the named account
adapters use the same registry and persistent import tracking. See the account import instructions below.


### Named token and OAuth accounts

In **Configured sources → Add source**, choose a provider's **account** adapter
and give the connection a name. Multiple accounts for the same service have
independent credentials, indexed evidence and sync checkpoints. The existing
single-connection cards remain available during migration.

Gmail, Google Drive, Calendar, Contacts and Tasks, Spotify and Strava support
**Authorize account** through the existing secure browser broker. Google consent
requests only the selected product's read scope. Each named connection has its
own callback URL: `/v1/sources/{source_id}/oauth/callback`. Register the exact
public HTTPS URL (loopback HTTP is permitted for local development) with the
provider. **Configure OAuth application** stores a client ID and client secret
in this connection's encrypted bundle; an existing server application
registration can also be reused. No centralized identity directory is required.
Application registration does not authorize an account by itself.

Slack, Dropbox, Granola, Oura, GitHub Notifications and Weather support a token
or API key entered on their connection card. Slack requires a user token;
Weather also needs a location in the source configuration. Token storage does
not contact the provider or prove access. Choose **Sync** to check access and
index documents. Provider support and token availability depend on the service.

Access/refresh tokens and application secrets stay in the server vault. Source
configuration and connection status return no secrets. OAuth attempts bind to
one source revision, callback and browser; refresh updates only that instance's
bundle. Sources never borrow a legacy Google account's tokens. Secrets entered
in the web form are cleared after the operation and are not put in URLs or
browser persistent storage.

Replacing a token, application registration or OAuth authorization, or choosing
**Disconnect this account**, clears that connection's index and checkpoint.
This prevents evidence from a previous account surviving an account change.
Sync again after authorization. These changes advance the source revision,
invalidate old consent attempts, and record metadata-only audit events. Other
connections and original provider data are unaffected. Disconnect removes local
authorization; it does not revoke the provider-wide grant. Removing the source
also removes its encrypted bundle and pending consent attempts.

Named account adapters use strict staged readers. They do not infer provider
deletions from omitted items. Named reads stage up to 10,000 documents / 32 MiB
of text before indexing; incomplete traversals and reader/staging limits fail
without yielding a partial batch. Full provider-specific snapshot/deletion
contracts remain separate roadmap items.
No legacy connection is silently converted or disconnected.

Named Spotify and Strava sources use stricter readers. Each source's web form
lets you set **Max pages** (default 100, maximum 250), **Max documents** (default
5,000, maximum 10,000) and **Timeout seconds** (default 120, range 10–300).
Each response is limited to 2 MiB and the whole scan to 16 MiB. A rate limit,
failed page, malformed response, repeated record, unsafe cursor, cancellation or
limit breach fails the entire read before indexing and preserves the previous
successful checkpoint. Increase a limit and retry when the account exceeds it.
Existing saved sources with empty configuration use these defaults.

Spotify walks older pages using the provider's exclusive millisecond `before`
cursor, stopping at the prior successful scan time with a one-second overlap;
the first scan starts with a one-day window. Coverage is **provider-available
recent history**, not a lifetime listening archive. Strava reads every accessible
activity page on every sync, including older activities: activity start dates
cannot identify later edits or uploads. Its page-number inventory is not an
atomic snapshot; later scans can pick up changes made during a traversal.
An empty Strava page ends enumeration; a short page does not. Provider permission
filters still apply, and neither reader treats omitted records as deletions.
Documents carry coverage, fetch time, scan bound and content-version metadata.
Legacy connector endpoints retain their existing behavior.

Provider contracts: [Spotify recent history](https://developer.spotify.com/documentation/web-api/reference/get-recently-played)
and [Strava activities](https://developers.strava.com/docs/reference/#api-Activities-getLoggedInAthleteActivities).

Named Slack sources now use a strict reader too. Configure **Max conversations**
(default 200, maximum 2,000), **Max requests** (default 500, maximum 2,000),
**Max documents** (default 5,000, maximum 10,000), and **Timeout seconds**
(default 120, range 10–300) in the source web form. Empty saved configurations
use these defaults. Responses are limited to 2 MiB each / 16 MiB per scan;
the user directory has a fixed 10,000-user limit.

Each sync enumerates users and accessible public/private channels, DMs and group
DMs, including archived conversations, then reads history and thread replies.
It rereads available history rather than filtering by creation time, so edits to
older messages can replace their indexed version. Human, bot and block/file-only
message content is retained; file bodies are not downloaded. A failure in any
channel or thread, unfinished pagination, repeated cursor/record, rate limit,
cancellation or limit breach fails the whole read before indexing. Retry after
the rate limit clears or raise a configured limit as needed.

Coverage is **provider-available conversation history and threads**; workspace
retention and token scopes can limit it. A provider-reported history limit is
recorded as `provider_history_limited` on that conversation's documents. These
are not atomic provider snapshots, and omitted messages do not imply deletion.
Message identities, thread links, fetch time, scan bound and content versions
remain available for evidence attribution. Legacy Slack endpoints retain their
existing behavior. Pagination follows Slack's
[collection contract](https://docs.slack.dev/apis/web-api/pagination/) and
[history](https://docs.slack.dev/reference/methods/conversations.history/) /
[thread](https://docs.slack.dev/reference/methods/conversations.replies/) methods.

The remaining named readers—Gmail, Drive, Calendar, Contacts, Tasks, Dropbox,
Granola, Oura, GitHub Notifications and Weather—now use adapter version 2. For an
existing version-1 source, use **Preview configuration upgrade**, then
**Apply configuration upgrade**, and
sync again. The preview discloses an index reset; applying it clears that source's
old evidence and checkpoint, retains its encrypted authorization, and advances
the source revision. This prevents older conflated record identities or partial
indexes from mixing with the new scans. New sources and legacy imports start
at version 2; importing still neither contacts providers nor reuses old indexes.

Their web forms expose **Max requests** (default 500, maximum 2,000), **Max
documents** (default 5,000, maximum 10,000), and **Timeout seconds** (default 120,
range 10–300). Each response is limited to 2 MiB and the full read to 16 MiB.
Missing scopes, rate limits, failed detail/export/download requests, malformed
pages, repeated records/cursors, cancellation and limits fail the whole read
before indexing and preserve the last successful watermark. Limit errors name
the exceeded bound without exposing credentials. Raise an adjustable limit and
retry when necessary. Inventories/windows are reread to capture older edits;
neither provider mutations during pagination nor omissions imply an atomic
snapshot or authorize deletion.

| Reader | Coverage and behavior |
| --- | --- |
| Gmail | Listed messages, including Spam/Trash; full message bodies and paginated inventory. File attachments are not downloaded; separately stored text bodies are read. Received timestamps come from `internalDate`. |
| Drive | Nontrashed files in the user corpus, including supported shared-drive items. Incomplete searches fail. Docs/Sheets/Slides are exported; other MIME types remain explicitly metadata-only. Failed exports do not replace text with metadata. |
| Calendar | All accessible calendar-list pages and event-resource pages. Recurring masters are retained without expanding infinite series. Calendar IDs scope event identities. |
| Contacts | All connection pages. When no provider timestamp is available, the timestamp is explicitly the observation time. |
| Tasks | All task-list/task pages, including completed, hidden and assigned tasks. List IDs scope task identities. |
| Dropbox | Recursive available inventory. Text extensions are downloaded at their listed revision and verified against response metadata; other formats are metadata-only. Stable file IDs survive renames. |
| Granola | Accessible summarized notes plus every transcript page. Detail calls omit inline transcripts to avoid the inline transcript-size limit. Unprocessed notes excluded by the provider remain outside coverage. |
| Oura | Sleep, readiness and activity pages in a configurable UTC date window. **Lookback days** defaults to 30 (range 1–3,650), reread each sync. Separate record IDs preserve multiple sleep sessions on the same day. |
| GitHub Notifications | Available read and unread notifications, bounded by scan start; validated next-page links remain on the notifications endpoint. |
| Weather | Current observation plus four forecast intervals, staged together. Provider timestamps and interval times are retained; incomplete/malformed forecasts fail. |

Weather keys remain in the vault and are injected into the pinned HTTPS request
only after destination validation. They never appear in source URLs, evidence
metadata, response request URLs or public status/error messages. Query-authenticated
requests never follow redirects. Other provider credentials remain in trusted,
origin-bound headers. Legacy connector endpoints retain their existing behavior.

Provider contracts: [Google APIs](https://developers.google.com/workspace),
[Dropbox](https://www.dropbox.com/developers/documentation/http/documentation),
[Granola notes](https://docs.granola.ai/api-reference/get-note) and
[transcripts](https://docs.granola.ai/api-reference/get-transcript),
[Oura v2](https://api.ouraring.com/v2/docs),
[GitHub notifications](https://docs.github.com/en/rest/activity/notifications), and
[OpenWeather](https://openweathermap.org/api).


### Named IMAP mailbox connections

Choose **IMAP mailbox** in Data Sources and save its host, port, transport security
and mailbox (default `INBOX`). Then store its username and password on the source
card and sync. Use a provider app password when required; some providers require
OAuth instead of allowing password logins. Credentials are encrypted in the
server vault and never returned by the source API. The password field is cleared
after each submission. Usernames and passwords use printable ASCII; passwords
may include spaces. Replacement/disconnection clears this source's indexed
evidence and checkpoint; resync after replacement. Source removal deletes its
vault binding. Configuration edits follow the existing preview/reset workflow.

TLS defaults to port 993; STARTTLS defaults to 143 and must succeed before login.
Certificates and hostnames are verified. DNS-resolved addresses are pinned.
**Destination access** defaults to **Public mail server**, where all addresses
must be public unicast. For a LAN mail server, choose **Authorize private LAN
mail server** and enter its exact **Authorized LAN IP addresses**, separated by
commas or whitespace. For example, authorize `192.168.1.20` for `mail.internal`
and port 993. Every DNS result must match this source's list on every connection;
an unlisted or public result blocks the entire connection. Authorize up to 32
exact IPv4 RFC1918 or IPv6 unique-local addresses. CIDR ranges, localhost,
loopback, link-local/metadata destinations and IPv6 zone identifiers are rejected.
These settings permit only the configured source's IMAP host/port, without
changing public HTTP or legacy connector policies.

**Certificate trust** defaults to system certificate authorities. If the mail
server uses your private CA, select **This source's private CA** and paste its
public PEM CA certificate(s) into **CA certificate (PEM)**. Up to eight CA
certificates and 16 KiB are accepted; private keys and leaf certificates are
rejected. Only these roots are trusted for that source. Certificate and hostname
verification remain mandatory for TLS and STARTTLS. Use the certificate's DNS
name as the IMAP host, even when you authorize its numeric LAN address.

Existing version-1 IMAP sources require the card's explicit configuration upgrade
before further syncs. The preview adds public/system-trust defaults and preserves
the password, existing index and checkpoint. Afterwards, LAN authorization or
certificate-trust changes use the normal configuration edit/reset workflow;
resync to rebuild that source's evidence. Adding or editing a source requires
authenticated API access; configuration does not grant tool capabilities.

Mailbox names must be printable ASCII (use the
server's modified UTF-7 wire name for non-ASCII folders). Save separate named
connections for different mailboxes or accounts.

The scan rereads every UID in the configured mailbox using read-only selection
and `BODY.PEEK[]`; it does not mark messages read, delete, move, expunge or send
mail. Message identities include mailbox, UIDVALIDITY and UID, scoped to the
source instance. INTERNALDATE supplies the provider timestamp. Plain text and
HTML bodies are indexed; attachments and attached message bodies are excluded.
Messages without inline text retain subject/from evidence with an explicit body
absence marker. No missing message implies deletion, and scans do not claim an
atomic mailbox snapshot. A changed UIDVALIDITY is blocked against the saved
checkpoint; replace authorization (which resets the index) and resync so old UID
namespaces cannot mix with replacement mailbox records.

**Max messages** defaults to 1,000 (maximum 10,000); **Timeout seconds** defaults
to 120 (range 10–300). The transport bounds each message literal to 2 MiB and all
received data to 16 MiB, including protocol responses. The socket timeout is at
most ten seconds and cancellation is checked between reads/commands. All reads
are staged: failed login, missing/mismatched bodies, invalid UID inventories,
mailbox identity changes, malformed MIME, cancellation or limits fail before
indexing and preserve the last successful checkpoint. Raise a configurable
limit, choose a smaller mailbox, or correct authorization and retry. The Test
configuration action validates settings locally; sync verifies actual access.
Existing saved IMAP connections can be imported explicitly using the workflow
below; the original connection remains intact.

### Import other existing account connections

**Configured sources → Import existing connections** now offers imports for
Gmail, Google Drive, Calendar, Contacts, Tasks, Spotify, Strava, Slack, Dropbox,
Granola, Oura, GitHub Notifications, Weather, Email (IMAP) and Gmail (IMAP),
alongside Notion. An entry is
available when its legacy server credential bundle has the fields required by
that adapter. This checks stored format, not live provider access. Unsupported
or incomplete bundles remain unavailable; configure a new named account instead.

Choose **Import**, enter a name, review **Preview import**, and select **Apply
import**. Token credentials and complete OAuth bundles are copied into an
independent encrypted vault binding for the new source. OAuth refresh tokens and
application credentials are retained when present and complete. Access-only
OAuth tokens can also be imported; the review explains that new authorization
will be needed when the access token expires. Weather's location becomes normal
source configuration while its API key stays in the encrypted bundle. No token,
client secret, credential file path, or secret fingerprint appears in the preview.

Google imports prefer the product's existing credential file. Only when it is
absent do they use the legacy shared `google.json`, matching the old readers.
A present but unusable product file does not silently switch accounts. Preview
and apply bind to both the selected file and its credential snapshot; changing
which file is selected, refreshing/rotating the old credential, or disconnecting
it requires a fresh preview. GitHub Notifications recognizes the reader's
`github.json` file and the older `github_notifications.json` alias.

Import preserves the existing provider grant, including broader legacy Google
permissions; copying credentials does not narrow those permissions. The review
recommends reauthorizing the named account to request the adapter's current read
permissions. Copied OAuth credentials may still share a provider authorization
with the old connection. Provider revocation or refresh-token rotation can affect
both. After verifying the named source, retire the old connection to avoid
parallel syncs and duplicate evidence; local disconnect does not revoke the
provider-wide grant.

Preview leaves the original file unchanged, including old plaintext JSON. Apply
upgrades plaintext legacy credentials to encrypted vault references and makes a
separate protected copy. It makes no provider requests, starts no sync, and
neither imports nor deletes legacy indexed documents. Choose **Sync** to populate
the named source's fresh index. A failed source commit or marker write can be
retried using the same reserved source/bundle identities. Recovery never replaces
an independently changed reserved credential. Completed retries preserve later
named-source edits. Removing an imported source keeps an import tombstone and
prevents replay from recreating it; use **Add source** for a deliberate new one.

Each legacy integration has its own import tracking, so a shared Google
registration can provide separate named product connections. Import plans expire
after ten minutes and bind to the caller, trusted recipe, adapter version and
current credential selection. A new preview is required after expiry or relevant
changes. Credentials configured only through an in-memory constructor are not
server files and cannot be imported by this flow.

IMAP imports copy the saved email/login name and password into the named source's
encrypted username/password bundle. Password spaces are preserved. Saved
`imap_host`, `imap_security` and `imap_port` become normal source settings;
legacy TLS aliases and numeric port strings are normalized. Without a saved
host, Email (IMAP) uses the legacy email-domain mapping and Gmail (IMAP) defaults
to `imap.gmail.com`. Missing transport/port settings default to TLS/993 (or 143
for explicit STARTTLS). The old reader scanned INBOX, so imports use INBOX with
the named adapter's 1,000-message and 120-second limits. Other legacy fields are
not copied. Change settings through the named source's normal edit workflow.

The review shows the endpoint, mailbox and scan limits, without exposing the
username or password. Import does not verify login, TLS or mailbox access; Sync
does. Imported sources default to public destination access and system trust,
even if the old endpoint is private. Explicitly edit the named source's LAN
address authorization and certificate trust before syncing a private endpoint.
Password changes at the mail server can affect both connections. No
legacy email index is migrated or removed; the imported source starts with a
fresh independent index.

## Request and conversation diagnostics

HTTP responses expose server-generated `X-Request-ID`, `X-Turn-ID` and
`X-Trace-ID` headers. Use these IDs to locate an execution in the configured
trace store when tracing is enabled. IDs are also available to browser clients
through CORS. Sending your own identity headers does not select or overwrite
them.

Chat traces, saved conversation messages, and research SSE frames include
`correlation` metadata. Its `user_id` comes from verified authentication;
`session_id` and `conversation_id` refer to the same ownership-checked persistent
conversation. Requests without a persistent session leave both empty. Research
using only the shared server API key leaves personal identity empty. Each request
has distinct request/turn/trace IDs even when browser, desktop and Android share
one conversation.

When enabled, research saves a summary trace containing its final answer and
evidence verdict; worker failures retain a failure trace with the exception type.
These summaries do not represent a full research tool-step replay. Correlation
metadata contains identifiers, not credentials. Shared event-bus collection is
scoped to the execution so simultaneous requests and late tool workers cannot
mix their trace steps. Channel, scheduled operator and managed-agent executions
also carry correlation IDs; background executions do not claim a human identity.
Live client acceptance remains Phase 2 work.

WebSocket chat at `/v1/chat/stream` accepts an optional `session_id` on each
message, for example:

```json
{"message": "Continue our discussion", "model": "qwen3.5:9b", "session_id": "your-existing-session-id"}
```

Authenticate with a human login session to use a persistent conversation. A
shared API key alone cannot select one. The server checks ownership, loads the
same history used by HTTP chat, and saves the user message and completed answer.
The returned chunk/done/error frames include `correlation` with the verified
session and conversation IDs. Create conversations through the existing session
API; this endpoint does not create them automatically. Supply the ID again on
each message and after reconnecting. Omit it for a stateless turn.

The server rechecks authentication for every message, so logging out/revoking
the login prevents further turns on an already-open socket. If storing a message
fails, the socket returns an error instead of confirming completion. A failed or
interrupted generation may leave the submitted user message in history; partial
assistant output is not saved as a completed answer.
