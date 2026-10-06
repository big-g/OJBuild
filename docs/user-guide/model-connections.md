# Model server connections

Administrators can open **Settings → Models → Model server connections** to
save multiple Ollama servers, edit/remove connections, test installed-model
catalogs and view change history. Ordinary accounts and master API keys alone
cannot manage these shared settings.

To use a server for chat:

1. Add the connection and **Test catalog**.
2. For each model you want to use, select **Read capabilities**. This reads
   Ollama's `/api/show` manifest without running or loading the model.
3. Review the reported capabilities and select **Enable for chat**. At least
   one model must report `completion`. Only models with a recorded chat-capable
   manifest appear in the picker; embedding-only models stay excluded.
4. Open the installed model picker and select a model labelled with its server,
   for example `qwen3.5:9b — home_gpu`.

Enabled connections are shared inference resources for authenticated users.
The model list also requires a human session when it includes configured-server
models. Identical model names on different servers have separate stable IDs;
renaming a connection does not change its ID. Selection loads the model on its
server during the first inference request, without browser-local preloading.
Explicit selection works for standard chat, its server-agent path and Deep
Research. Existing `[deep_research]` overrides retain priority; an incompatible
explicit engine override is reported as a configuration error.

Use a name such as `home_gpu`: 1–24 lowercase letters, digits or underscores,
starting with a letter. The server URL is a root HTTP(S) URL, for example
`http://localhost:11434` or `http://192.168.1.20:11434`. Localhost means the
OpenJarvis backend machine, not the browser. IPv6 loopback and private unique-local
addresses are supported with brackets, for example `http://[fd00::1]:11434`.
Use explicit private LAN IPs for remote machines. Credentials, URL paths,
queries, public addresses, link-local/metadata endpoints and remote DNS names
are unsupported in this first adapter. HTTPS uses normal certificate verification;
custom CA and authenticated endpoints are later extensions. HTTP LAN traffic is
unencrypted: use a trusted LAN or verified HTTPS.

Remote Ollama must listen on its LAN interface and its firewall must permit
the OpenJarvis server to reach the configured port. Avoid exposing an unauthenticated
Ollama endpoint to the internet. Network setup remains an administrator task.

**Add Ollama connection** saves configuration only. **Test catalog** performs
one bounded `GET /api/tags` read, with a 10-second HTTP timeout, a 10-second
streaming budget, a 1 MiB decoded-body limit and at most 500 model entries.
Redirects and environment proxy settings are disabled. Test does not pull,
load or run models. An empty catalog is a successful result with no models.
Reported sizes are package sizes, not measured VRAM requirements.

Discovery records a timestamped snapshot of model serving IDs. **Read capabilities**
records provider reports, not behavioral verification: reported tool/vision support
does not prove reliable results or sufficient GPU memory. Capability reads use the
same bounded JSON transport as catalog discovery. Missing/malformed manifests fail
visibly, including older Ollama versions that omit capabilities.

Every new request and subsequent model call checks the current enabled connection
revision and a live manifest before generation. Required tool/image capabilities
must be present. Tool requests do not retry without tools on provider rejection.
Unavailable models do not route to another server or a cloud provider. Guardrail,
telemetry and evidence checks remain in place; agent engine/model overrides are
restored under the shared-agent lock after success or failure.

Edits, catalog tests and capability reads disable the connection, requiring explicit
re-enablement after review. Failed catalog tests clear the catalog; failed capability
reads clear that model's manifest. Revision checks prevent stale writes/tests from
overwriting newer configuration. Disabling/removing a server blocks later model
calls in an active run; an inference already submitted may finish. A missing picker
selection remains selected until the user chooses another model, rather than silently
falling back. Change history survives removal; deleting a connection does not delete
model files from Ollama. Manage model files on the remote Ollama server itself.

The database persists across restarts at `~/.openjarvis/model_connections.db`
by default. Set `[security].model_connections_db_path` to relocate it, then
restart the existing OpenJarvis service. Upgrades apply the dedicated database
schema migration; newer unsupported schemas fail visibly rather than being
silently rewritten. Up to 64 connections can be saved.

Saved connections do not alter `config.toml` or the server's default inference source.
New and migrated connections start disabled. Managed-agent and scheduled selections,
versioned behavioral diagnostics, durable routing traces and optional explicit
fallback use the same saved connections. Parallel multi-model execution remains
a separate later objective; existing shared-agent requests remain serialized.

## Task assignments and diagnostic measurements

An administrator can open **Settings → Model task assignments and diagnostics**.
Choose an enabled model and one of **general**, **coding**, **analysis** or
**vision**, then select **Run diagnostic**. Review the pass/fail result, elapsed
time, provider-reported token count and connection revision. Select **Assign and
enable** to persist that exact result and model as the task assignment. Assignments
are shared server configuration; changing them requires an administrator session.

In standard chat, **Model choice** defaults to **Manual model**, which uses the
existing picker. Choose a task assignment to use the assigned model for that
message. The task is explicit; the server does not guess from your prompt or rank
models automatically. This applies to non-streaming chat, streaming and its
server-agent path and WebSocket chat. Managed agents can choose the same task
assignments in **Intelligence**; their immediate and scheduled ticks use that choice.
General scheduler tasks accept `metadata.model` as a saved `oj/...` identity or
`task/general`, `task/coding`, `task/analysis`, or `task/vision`. Deep Research retains
its explicit planner configuration. The response reports the chosen model and routing reason; chat
telemetry shows the actual serving model and the reason in the expanded footer.

Diagnostics use three versioned, fixed cases per task. General checks alphabetical
ordering, extraction and constraints; coding checks tracing, mutation and boundary
values; analysis checks logic, weighted calculations and dependencies; vision checks
solid colors, left/right position and region count with deterministic PNG fixtures. If a
model reports tool support, a separate probe checks one exact synthetic function
call. Returned code and tool calls are never executed, and no personal messages,
source documents, credentials or real tools enter a probe. Diagnostic reads use
the bounded JSON transport (1 MiB, 10-second read timeout and elapsed stream
budget); output budgets are 512 tokens per case and 128 for the canary. No new
request starts after the 45-second suite budget; an in-flight bounded request may
finish after that budget.
One diagnostic runs at a time per API process. Probes may load a model and consume
GPU resources; a cold or busy model can fail the bounded availability budget.

These are **small diagnostic probes, not a broad quality ranking** or proof of
reliable real-world coding/reasoning. Elapsed time includes live checks, loading,
generation and transport; it is not isolated generation throughput. Compare models
on the same hardware under similar load, and make the assignment yourself. Per-case
results and latency appear in diagnostic history. These checks are not a substitute
for evaluating your own coding and reasoning workloads. Discovery, passing a probe and
enabling an assignment grant no tool permissions and do not relax evidence gates.
Replacing model weights under an existing Ollama tag is not detected by these
diagnostics: retest the catalog, reread capabilities and rerun diagnostics after
updating a model on its server.

Only a passing latest result for the same task, model, suite and current connection
revision can enable an assignment. Connection edits, disable/re-enable or catalog/
capability reads invalidate that binding. A newer diagnostic result requires review
and re-assignment, even if it passes; a failed repeat cannot reuse an older pass.
Later calls in an active agent run recheck the task revision and benchmark binding.
An already submitted generation may finish. Missing/stale rules fail visibly and
never substitute the manual selection or another server. Tool-using requests also
require a passing tool-call canary; image requests require the Vision assignment.

Assignment revisions and administrator audit events persist alongside benchmark
history in the existing model connection database (schema version 5). Existing
connections retain their activation state; all new task assignments start disabled.
The screen shows the latest 100 benchmark results and assignment audit events.

## Explicit fallback and durable traces

Fallback defaults to off. Run this task's diagnostics for both models, review the
latest passing results, select **Optional fallback**, and choose **Assign and enable**.
The fallback must be a different saved model with a passing current result for the
same task. Selecting **No fallback** and saving removes an existing alternative.

Only a transport outage during the initial primary capability preflight qualifies.
The system selects at most one reviewed alternative, checks its live capabilities,
and binds the whole run to it. Missing tools/vision support, stale approvals, malformed
provider responses, generation errors and interrupted streams fail visibly. They
never trigger a new selection. Managed tick retries retain the initial binding.
Rule, connection and benchmark changes still block later calls in that run.

Trace metadata records the actual selected model, task, rule and connection revision,
benchmark identity and routing reason. Fallback decisions also identify the original
primary model and selected fallback benchmark. It grants no new tool or source access.
The active safety/telemetry chain is preserved even when nested in MultiEngine.

The `behavior-v2` suite replaces the earlier single-question diagnostics. Existing
server activation is retained, but task assignments with old-suite results cannot run
until an administrator reruns diagnostics and saves the current passing result.
