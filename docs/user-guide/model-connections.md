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
New and migrated connections start disabled. Dedicated managed-agent/scheduled
configuration, task assignments, behavioral benchmarks, routing explanations and
explicit fallback rules are the next steps. Parallel multi-model execution remains
a separate later objective; existing shared-agent requests remain serialized.
