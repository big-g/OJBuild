# Service APIs for data and analysis

**Data Sources → Add API connection** opens a database-backed service editor.
A versioned service definition contains a base origin and named operations;
each source selects one operation, its typed inputs and a protected credential.
Settings are stored in the server database, not `config.toml`. Existing JSON API
connections remain compatible and continue using their original editor.

## Configure a service

1. Select an example (Weather.gov, Open-Meteo, GitHub, Notion or a generic API),
   load one of your saved templates, or enter a service base URL. The base URL
   contains the origin only, such as `https://api.example.com`.
2. Select an operation and enter its inputs. Inputs support strings, numbers,
   integers, booleans and string lists, with required fields, defaults, choices,
   bounds and dependencies. Lists use comma-separated or repeated query values.
   Path inputs are escaped separately; query and body inputs retain their types.
3. Configure non-secret service and operation headers, such as `User-Agent`,
   `Accept` and provider version headers. Operation headers override service
   headers. Authentication and transport headers cannot be entered here.
4. If authentication is required, add a **Protected credential** and select it:

   | Authentication | Protected value | Placement |
   | --- | --- | --- |
   | Bearer | Provider-issued token | `Authorization: Bearer …` |
   | API key header | Provider-issued key | Configured header, such as `X-API-Key` |
   | API key query parameter | Provider-issued key | Configured parameter, injected only on the server |
   | HTTP Basic | `username:password` | Encoded `Authorization: Basic …` |

   Credentials are encrypted, scoped to an HTTPS origin and returned only as
   metadata. Query keys never become part of saved/displayed request URLs;
   query-authenticated redirects are rejected. Neither Bearer nor Basic encoding
   converts an API key into an OAuth token. Back up the vault key with its database.
5. Open **Request and response configuration** to edit the endpoint, parameters,
   body, mapping, pagination or linked reads. GET and explicitly declared
   read-only POST requests can be indexed. JSON, form, GraphQL, text and XML
   request bodies are supported; `"{{input_name}}"` preserves the input's type
   when it occupies a whole JSON value. Use `in: "variable"` for inputs referenced
   only through body placeholders, including GraphQL variables.
6. Select **Test connection**. Jarvis validates the complete bounded scan without
   saving/indexing it, displays a method/URL/status/type/size request trace and
   up to three mapped samples (4,096 characters each). Previews are escaped text;
   HTML, scripts and XML are never executed. Invalid, incomplete or credential-
   reflecting scans fail. Editing the configuration clears the old preview.
7. **Save source**, then **Sync**. Existing scheduling, ownership, administrator
   approval for universal sources, per-account consumption and attributed
   knowledge retrieval apply. Indexed data reflects the last successful sync;
   a test is not a live chat tool or an automatic alert rule.

## Response mapping

Responses support JSON, CSV, XML and plain text. Choose a whole document, a
record collection with stable IDs, or aligned time-series arrays. JSON pointers
are case-sensitive: `/items` selects a field, an empty pointer selects the root,
`~1` escapes `/` in a key and `~0` escapes `~`. CSV becomes an array of objects
keyed by unique column headings. XML can select record elements using `xml_path`,
for example `./item`; DTDs and entity declarations are rejected.

For a record collection, configure `records_pointer`, `id_pointer` and optional
`title_pointer`/`content_pointer`. IDs must be unique within the complete scan.
For time series, configure `time_pointer` and a `columns` map whose arrays have
matching lengths. Units and timezone pointers preserve their values in indexed
content and provenance. Changing requested weather variables also requires
updating the column mapping; Jarvis does not invent missing values.

The Weather.gov example first reads `/points/{latitude},{longitude}`, follows
`/properties/forecast` on the same origin and maps `/properties/periods` by
`/number`. Set its User-Agent to include your application/contact details.
The Open-Meteo example exposes coordinates, hourly variables, timezone,
temperature units and forecast days; it aligns hourly values with timestamps.

Pagination supports Link headers, a next-URL pointer, cursor parameters, page
numbers and offsets. Configure `max_pages`, parameter placement (`query`,
`body` or GraphQL `variables`), and the continuation pointer as appropriate.
Page/offset scans start at the configured `start`. A full page implies another
page unless `has_more_pointer` supplies the provider's explicit completion flag.
All linked requests, redirects and continuation URLs stay on the configured
origin. A repeated token, invalid page or unfinished scan at the page limit
fails before any documents are yielded for ingestion.

GraphQL responses containing `errors` fail even when HTTP status is 200.
Other providers can supply an `error_pointer`; accepted HTTP status codes are
configurable 2xx values. HTTP 204 represents an empty successful response.
Authenticated errors use a generic message so provider error bodies cannot
expose credentials.

## Templates and imports

Save reusable definitions as personal database-backed templates. Templates
contain no connection inputs or credential references. Loading a template makes
a detached draft: edits never silently change existing sources. The backend
checks template ownership and revisions when updating/removing them.

The editor imports service JSON, **OpenAPI 3 JSON** and a limited **cURL** syntax
as drafts. Import never executes a shell command, resolves remote references,
contacts a service or grants tool authority. Remove secrets before pasting.
OpenAPI import covers endpoints and query/path inputs; resolve parameter
references first, then review request bodies, security and response mapping.
POST/PUT/PATCH/DELETE imports are classified as actions. The operator must
review whether a POST is truly a read before explicitly changing its kind to
`read`; action definitions cannot be tested or synced through this adapter.
Export writes only the portable service definition.

## Limits and extension points

Definitions and request bodies are limited to 64 KiB, with 32 operations,
64 inputs, 24 headers, three linked reads, 50 pages and 1,000 mapped records per
scan. Fetches are capped at 2 MiB; complete scans at 10 MiB and 60 seconds.
Public HTTP/HTTPS destinations are validated and DNS addresses pinned; private
LAN destinations require a dedicated approved adapter.

This adapter does not perform writes, OAuth authorization, AWS request signing,
multipart/file uploads, WebSocket/SSE streams, cross-origin workflows, automatic
retries, cache revalidation or provider-specific incremental/deletion contracts.
Use existing provider adapters for their OAuth and incremental-sync support.
These features can be added through adapters without replacing the versioned
service definition, source ownership or encrypted credential foundations.
Live provider acceptance remains a separate check: the automated tests use
recorded-style fixtures and never contact external services.

## Ask for feedback

Ask Jarvis to use your synced source, cite the data, retain units and timestamps,
and identify missing information. Fetch time states when Jarvis read a response;
it does not establish when the provider measured it. Model reasoning continues
through the existing evidence and tool authorization checks.

Connections are personal by default. Administrator-approved universal sources
can be shared across accounts, while each user chooses which sources to use.
Consumers cannot see the owner's configuration or credentials. See
[Personal and universal sources](shared-sources.md).
