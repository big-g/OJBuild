# Model server connections

Administrators can open **Settings → Models → Model server connections** to
save multiple Ollama servers, edit/remove connections, test installed-model
catalogs and view change history. Ordinary accounts and master API keys alone
cannot manage these shared settings.

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

Discovery records a timestamped snapshot of model serving IDs. Capabilities
remain **unverified**: model presence does not prove tool calling, vision,
inference health or available GPU memory. Editing or a failed test clears the
snapshot. Revision checks prevent stale edits/tests from overwriting newer
configuration. Each server retains its own stable identity, including when
model names match. Change history survives removal; deleting a connection does
not delete models from Ollama.

The database persists across restarts at `~/.openjarvis/model_connections.db`
by default. Set `[security].model_connections_db_path` to relocate it, then
restart the existing OpenJarvis service. Upgrades apply the dedicated database
schema migration; newer unsupported schemas fail visibly rather than being
silently rewritten. Up to 64 connections can be saved.

This batch establishes configuration and catalog discovery. Saved connections
do not alter `config.toml`, the existing chat selector or inference routing.
Explicit selection across configured servers, verified capability checks,
task assignments, selection traces and bounded fallback are the next steps.
Parallel multi-model execution remains a separate later objective.
