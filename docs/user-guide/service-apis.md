# Service APIs for data and analysis

Use **Data Sources → Add API connection** to configure non-AI services that
return JSON through HTTP GET. This opens the existing database-backed JSON API
adapter; it does not add a duplicate connection store or write `config.toml`.
Each named connection keeps its own settings, index and sync checkpoint.

## Configure and verify a connection

1. For an authenticated API, open **Protected credentials → Add credential**.
   Enter a descriptive name, the HTTPS origin (for example
   `https://api.example.com`, without a path/query), and the raw credential.
   Choose Bearer token for `Authorization: Bearer KEY`, or API key header for
   a provider-specific header such as `X-API-Key: KEY`. The credential is
   encrypted and restricted to that origin; neither choice converts an API key
   into an OAuth token.
2. Select **Add API connection** and name the source, such as `Home readings`.
   Enter its data endpoint, for example
   `https://api.example.com/readings?station=home`. Non-secret query parameters
   are supported. Never put credentials into the URL. Select the protected
   credential if needed; otherwise choose No authentication.
3. Choose **Whole JSON document** for a single payload, or **Individual records**
   for a collection. With the following response, use `/items` for the array,
   `/id` for stable IDs, and leave the content pointer empty to preserve each
   whole record:

   ```json
   {
     "items": [
       {"id": "sensor1", "temperature": 21, "unit": "C",
        "measured_at": "2026-10-06T00:00:00Z"}
     ]
   }
   ```

   Paths are case-sensitive. An empty array pointer selects a root array; an
   empty content pointer keeps the whole record. Escape `/` inside a key as
   `~1` and `~` as `~0`. Titles are optional; IDs must remain stable across edits.
4. Select **Test connection**. Jarvis validates the complete bounded scan and
   displays up to three mapped samples, with at most 4,096 characters each.
   The preview is escaped text: scripts/HTML are not executed or embedded.
   A truncated preview is labelled; malformed/unfinished scans fail the test.
   Credential-reflecting responses are rejected. Tests do not save settings or
   index data, and editing the form clears the old preview.
5. **Save source**, then **Sync**. For changing data, enable **Scheduled sync**
   on its card and set the interval. Ensure **Use this source for my account**
   remains selected. Syncing makes the returned data available to Jarvis's
   ownership-filtered knowledge search and reasoning tools.

## Ask for feedback

In a tool-enabled chat, ask a concrete question such as:

> Using my Home readings source, explain the latest temperature readings and
> cite the data. Identify missing information before making recommendations.

Preserve units and measurement timestamps in the data being indexed. Jarvis
records the source, requested/final URLs and fetch time for attribution. Fetch
time describes when Jarvis read the API; it does not prove when a measurement
was taken or that the provider's data is current. Indexed results reflect the
last successful sync. Model reasoning still passes the existing evidence checks;
configuration alone does not establish accuracy or authorize fabricated facts.

Connections are personal by default. Administrator-approved universal sources
can be shared across accounts, while each user chooses which shared sources to
use. Consumers cannot see the owner's configuration or credentials. See
[Personal and universal sources](shared-sources.md).

## Supported scope

The generic adapter supports public HTTP/HTTPS JSON GET endpoints, protected
Bearer/API-key headers over HTTPS, bounded pagination and existing interval
syncs. Responses are limited to 2 MiB per fetch; record/page limits are configured
in the form. Saving an endpoint does not run scripts or perform write operations.
Scheduled sync updates the knowledge store; it does not automatically send alerts.

Private LAN APIs, XML/other response formats, general-service OAuth flows,
provider-specific validation and operations that change data require additional
adapters. Prefer an existing dedicated provider adapter when it supplies the
provider's authorization and data-validation contract.
