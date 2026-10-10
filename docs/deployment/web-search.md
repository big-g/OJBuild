# Keyless web search and forecast verification

Weather questions use the existing `web_search` tool. OpenWeather credentials
are not required. `TAVILY_API_KEY` is optional: without it the tool skips Tavily
and tries DuckDuckGo, Bing and Brave explicitly, in that order. Empty or failed
providers fall through to the next provider. A configured Tavily provider also
falls back if it returns no usable source text.

For keyless results the tool reads up to two public result pages by default,
with a shared 16-second page-read budget and a 2 MiB limit per page. It preserves
paragraph boundaries, discards scripts and records source URLs and retrieval
timestamps. Failed page reads retain the original search snippets, not invented
forecast details. `fetch_pages=false` requests snippets only. Source requests
use the existing DNS-pinned public HTTP transport, including redirect checks.

Search-provider requests use an 8-second provider timeout. The tool executor
allows 75 seconds for provider fallback and page reads; exhausted or failed
searches return failure with no evidence records. Provider attempts are included
in result metadata, without echoing provider exception messages or credentials.
Search engines can still rate-limit or block requests.

The tool description directs US forecast searches toward National Weather
Service pages and asks the model to check forecast validity dates. Cached search
snippets may be stale; a newly executed search is not itself a fresh forecast.

If an orchestrator model answers an evidence-required question before calling
an evidence provider, it gets one retrieval reminder within its existing turn
limit. Both function-calling and structured modes retain ordinary tool
governance. Models that ignore the reminder, failed tool calls and unsupported
answers remain subject to the existing evidence gate.

## Update the existing server

Finish active generation jobs before restarting OpenJarvis. No frontend rebuild
or weather connector setup is needed for this change.

```bash
cd ~/.openjarvis/src
git pull --ff-only origin main
sudo systemctl restart openjarvis-api.service
uv run python scripts/check_web_search.py
```

The probe deliberately disables Tavily and prints configured tool selection,
provider attempts, source URLs, whether content is page text or a search snippet,
and an excerpt. It tests transport only, not authenticated server dispatch,
capability grants, model invocation, relevance or final answer grounding.

Ask in the browser: `What is the forecast for Kernersville NC?` Verify that the
tool runs, the retrieved text contains actual dated forecast details, and the
answer is supported by those details. Repeat during a Hunyuan job to verify the
background chat model follows the same retrieval path.

If chat still blocks, collect the probe output and recent service logs:

```bash
journalctl -u openjarvis-api.service --since '10 minutes ago' --no-pager -n 120
```

`provider=... failed (...)` identifies transport failure. A transport probe
which succeeds while chat never invokes `web_search` points to runtime tool
selection, model behavior or governance, rather than requiring a weather API.
Successful search or populated evidence metadata alone is not a forecast
quality verdict: relevance, dates and answer grounding still need verification.
