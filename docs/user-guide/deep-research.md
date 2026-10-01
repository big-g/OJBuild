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
These adapters contain no secret fields. Protected credential references,
scheduled jobs, cancellation/progress, and authenticated APIs remain roadmap work.

### Public Web Page and JSON API sources

**Web Page** indexes one public HTML, plain-text or Markdown URL. It extracts
readable HTML text and a page title, excludes script/style/template and explicitly
hidden elements, and does not execute JavaScript or follow page links. Add one
named connection per page. For browser-rendered sites and crawling, the existing
Playwright/Scrapy tools remain separate governed operations.

**JSON API** fetches a public JSON endpoint with GET. The default **Whole JSON
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
snapshot**, records absent from one response remain indexed. Automatic pagination
and authenticated endpoints are not supported in this step.

**Test connection** actually fetches and parses either public source without
saving/indexing it. Fetching accepts public HTTP/HTTPS addresses on ports 80/443,
pins verified DNS addresses while keeping TLS hostname verification, revalidates
redirects, and rejects private/metadata addresses and HTTPS-to-HTTP downgrades.
There are at most five redirects, four address attempts per destination, a 2 MiB
response limit, 20-second I/O timeouts and a checked 60-second fetch budget.
Compressed, partial, failed and unsupported-format responses report errors.
Private services belong in explicitly scoped integrations rather than these
public adapters. These sources send no credentials, cookies or custom headers;
credential-bearing URLs and recognized credential query parameters are rejected.
Do not put secrets into a public URL.

Indexing records the original URL, final URL, response hash, fetch time and source
instance identity. Search evidence retains the actual fetch timestamp, so a search
does not make cached web content appear newly fetched. Failed syncs retain the
prior successful watermark and do not reconcile missing documents.

Tool reads require the selected adapter's `connector:web_page:read` or
`connector:json_api:read` capability plus `network:fetch`. Saving a source grants
none of these capabilities. Existing Local Files permissions are unchanged.
