# Administrator server settings

Sign in as an administrator and open **Settings → Models → Administrator server
settings**. These settings are shared by every account on this Ubuntu backend.
Ordinary accounts cannot read or change the administrator settings API.

Choose **Server default model** from the installed existing-backend catalog.
Choose **Memory extraction model → Follow server default** to use that same model
for background fact extraction. Save applies both choices to future requests/jobs
without restarting. An in-flight extraction keeps its original model binding.
Using a different extraction model can cause Ollama to unload the chat model to
make room on the GPU. Explicit chat selections, personal model preferences and
verified task assignments still take precedence for their requests.

The server default is advertised in the model catalog, so clients with no explicit
model choice select it. Existing selections are retained; changing the server
default does not silently replace another user's chosen model. Configured-server
model identities currently use the task-assignment screen, rather than these
existing-backend default selectors.

Expand **All server parameters** and search a parameter name. Scalar OpenJarvis
configuration fields and lists of scalar values have generated controls. Types
and supported ranges are shown next to fields. Lists use JSON array syntax.
Other than model defaults, these values take effect when OpenJarvis restarts;
**Saved; restart required** distinguishes pending values from active values.
Changing a host, port or authentication setting can change where/how you reconnect
once the service restarts. This screen does not execute service-management commands.

Overrides are stored in `administrator-settings.db` beside the active TOML file.
TOML remains the bootstrap/fallback configuration and is not rewritten. DB values
are overlaid by `load_config()` on the next process startup. A CLI `--model` argument
still takes precedence during startup. **Reset to bootstrap** removes an override;
**Reload settings and models** refreshes current settings and catalog. Conflicting
saves from another administrator return a revision conflict instead of overwriting
newer changes. The DB records the actor, revision, time and changed field names;
it does not put setting values in the audit table.

Credential values are omitted and cannot be edited here. Use protected credential,
provider and integration screens. Dicts, lists of structured objects, optional
subsystems such as mining, and environment-only/Ollama daemon/OS settings are not
editable through this generic editor. Some legacy integration credentials still
require migration into dedicated protected screens. Do not put credentials into
ordinary text parameters; this is a settings store, not an encrypted vault.

## Configuration growth

Use a versioned descriptor, validator, persistence adapter and explicit application
policy for additional settings. Extend this schema/editor instead of adding
terminal-only configuration workflows. Remaining work includes structured legacy
configuration and credential migration, field-specific enums/semantic validation,
more live application adapters, configured-server defaults, environment-only
inference options, and a deployment-specific controlled service-restart facility.
