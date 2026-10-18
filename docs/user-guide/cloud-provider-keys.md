# Cloud provider API keys

Sign in as an administrator and open **Settings → Cloud provider API keys**.
Select a provider, paste its API key into the password field, and click
**Save provider key**. Select the same provider and save a new value to rotate it.
Use **Remove key** to disable its credential for new requests. No server restart
is needed after key changes. Reload providers if another administrator has edited
the settings since you opened the page.

The installed adapters support OpenAI, Anthropic, Google Gemini, OpenRouter,
MiniMax and DeepSeek. This screen adds credentials for supported inference
protocols; an API key alone does not implement a new provider's protocol or a
custom endpoint. OpenAI keys also apply to the existing OpenAI image tool, whose
operation authorization and account restrictions remain separate.

Keys are shared server configuration for all consoles. Ordinary signed-in accounts
can see which cloud providers are configured in the Cloud Models picker, but cannot
read or change credential settings or audit history. Master API keys alone cannot
manage provider credentials: a current administrator login is required. The cloud
picker directs key management to this server screen on both browser and desktop.

Saving a key does not switch the default model or send a paid test request.
Choose a cloud model explicitly in the model picker. “Configured” means a key is
present, not that the key, billing, model access or remote service has been verified.
The normal model, tool, evidence and content-policy gates still apply. New
requests use a new credential snapshot; already-running requests may finish using
the previous key. Removing a key does not cancel those requests or revoke the key
at the provider. Revoke it at the provider if necessary.

## Storage and legacy settings

The server reuses its encrypted credential vault (`source_credentials.db` and
`source_credentials.key`). `cloud-provider-keys.json` contains only a vault
reference. Saved keys are write-only; status and audit responses contain no key
values. Audit records identify the administrator, provider, save/remove action
and timestamp, retaining the latest 200 edits. Back up the database, encryption
key and reference together. A missing/unreadable vault key causes requests to fail
rather than silently falling back to old credentials.

Unmanaged providers can continue using environment variables or
`~/.openjarvis/cloud-keys.env`; the screen labels them as legacy configuration.
Saving or removing a provider creates an authoritative managed override that
survives restart. Removal therefore prevents an old environment/file key from
re-enabling that provider. The Google alias `GOOGLE_API_KEY` is also overridden by
managed Gemini settings. Legacy files/environment values are not rewritten; remove
those originals separately when retiring an old secret.

## Server packages

Governed cloud inference uses the corresponding SDKs. If a provider reports a
missing client/package during inference, install the existing server extras in
the environment used by `openjarvis-api.service`, then restart the service:
`inference-cloud` covers OpenAI-compatible and Anthropic SDKs, and
`inference-google` covers Gemini. Configured credentials do not install packages
or prove SDK availability. These package installations are separate from ordinary
key changes, which apply live.
