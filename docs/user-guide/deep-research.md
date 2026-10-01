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

### Web Page and JSON API sources

**Web Page** indexes one HTML, plain-text or Markdown URL. It extracts
readable HTML text and a page title, excludes script/style/template and explicitly
hidden elements, and does not execute JavaScript or follow page links. Add one
named connection per page. For browser-rendered sites and crawling, the existing
Playwright/Scrapy tools remain separate governed operations.

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
apply. OAuth flows, cookies and Basic auth are not yet implemented.

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
