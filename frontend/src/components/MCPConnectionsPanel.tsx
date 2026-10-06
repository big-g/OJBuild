import { useCallback, useEffect, useState, useId } from 'react';
import { connectionDefinition, mcpRequest, type MCPConnection, type MCPDefinition } from '../lib/runtime-mcp-api';
import { importLegacyMCP, reviewLegacyMCP, type LegacyMCPEntry } from '../lib/runtime-mcp-api';
import type { ToolAudit } from '../lib/runtime-tools-api';

const empty: MCPDefinition = { name: '', url: '', allow_without_confirmation: false };
const button = 'rounded-lg border px-3 py-2 text-sm cursor-pointer disabled:opacity-50';
const field = 'w-full rounded-lg border px-3 py-2 text-sm';
const style = { background: 'var(--color-bg-secondary)', color: 'var(--color-text)', borderColor: 'var(--color-border)' };

export function MCPCatalogReview({ connection }: { connection: MCPConnection }) {
  return <div className="space-y-3 text-sm">
    <p>Authentication: {connection.auth_type === 'api_key' ? `API key header (${connection.api_key_header})` : 'Bearer token (Authorization: Bearer)'} · {connection.has_token ? 'credential saved' : 'no credential saved'}</p>
    <p>{connection.allow_without_confirmation
      ? 'Approval allows calls without per-call confirmation, including chat and scheduled agents.'
      : 'Each call requires interactive confirmation. Clients and schedules without a confirmation callback will block calls.'}</p>
    <p>Required capability: tool:invoke. Server hints are untrusted and provide no factual evidence authority.</p>
    <p className="text-xs break-all">Review fingerprint: {connection.fingerprint}</p>
    <details><summary className="cursor-pointer">Review all {connection.tools.length} remote tool contracts</summary>
      <div className="space-y-3 mt-3">{connection.tools.map(tool => <article key={tool.name} className="border rounded-lg p-3" style={{ borderColor: 'var(--color-border)' }}>
        <p className="font-medium break-all">{tool.remote_name}</p>
        <p className="break-all text-xs">Jarvis name: {tool.name}</p>
        <p className="whitespace-pre-wrap">{tool.description}</p>
        <p className="mt-2">Input schema</p>
        <pre className="overflow-x-auto text-xs whitespace-pre-wrap">{JSON.stringify(tool.parameters, null, 2)}</pre>
        <p className="mt-2">Untrusted server annotations</p>
        <pre className="overflow-x-auto text-xs whitespace-pre-wrap">{JSON.stringify(tool.annotations, null, 2)}</pre>
        <p className="text-xs break-all">Contract digest: {tool.contract_digest}</p>
      </article>)}</div>
    </details>
  </div>;
}

export function LegacyMCPReview({ entries, busy, onImport }: {
  entries: LegacyMCPEntry[]; busy: boolean; onImport: (entry: LegacyMCPEntry) => void;
}) {
  return <div className="space-y-3">
    {!entries.length && <p className="text-sm">No legacy MCP entries configured.</p>}
    {entries.map(entry => <article key={entry.index} className="border rounded-lg p-3 space-y-2" style={{ borderColor: 'var(--color-border)' }}>
      <h4 className="font-medium">{entry.name || entry.label}</h4>
      {entry.url && <p className="text-sm break-all">{entry.url}</p>}
      <p className="text-sm">{entry.status === 'ready'
        ? `Ready to import · ${entry.has_token ? 'credential will be encrypted' : 'no credential'} · confirmation required`
        : entry.reason}</p>
      <button className={button} disabled={busy || entry.status !== 'ready'}
        onClick={() => onImport(entry)}>Import disabled connection</button>
    </article>)}
  </div>;
}

export function MCPConnectionsPanel() {
  const nameHelpId = useId();
  const authHelpId = useId();
  const headerHelpId = useId();
  const credentialHelpId = useId();
  const [connections, setConnections] = useState<MCPConnection[]>([]);
  const [legacy, setLegacy] = useState<LegacyMCPEntry[] | null>(null);
  const [definition, setDefinition] = useState<MCPDefinition>({ ...empty });
  const [editing, setEditing] = useState<MCPConnection | null>(null);
  const [token, setToken] = useState('');
  const [clearToken, setClearToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [audit, setAudit] = useState<{ name: string; events: ToolAudit[] } | null>(null);
  const load = useCallback(async () => {
    setConnections((await mcpRequest<{ connections: MCPConnection[] }>()).connections);
  }, []);
  useEffect(() => { void load().catch(e => setError(String(e.message || e))); }, [load]);
  const reset = () => { setEditing(null); setDefinition({ ...empty }); setToken(''); setClearToken(false); };
  const perform = async (action: () => Promise<unknown>, message: string) => {
    setBusy(true); setError(''); setNotice('');
    try { await action(); setNotice(message); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); await load().catch(() => {}); }
    finally { setBusy(false); }
  };
  return <section className="space-y-5 pt-8 border-t" style={{ borderColor: 'var(--color-border)' }}>
    <header><h2 className="text-lg font-semibold">MCP connections</h2>
      <p className="text-sm mt-2">Save a public or explicitly authorized LAN HTTPS connection, discover its tools, then review and approve the whole catalog.
        Approved tools are shared across accounts. Saving and discovery do not authorize calls.</p>
      <details className="mt-3 text-sm space-y-2"><summary className="cursor-pointer">What is MCP? Connection setup help</summary>
        <p>MCP (Model Context Protocol) lets Jarvis use tools provided by another service. Ask the service provider for its MCP HTTPS endpoint and, if required, an access token.</p>
        <ol className="list-decimal pl-5 space-y-1"><li>Choose a short connection name, such as <code>home_tools</code>. This is your label for the connection, not the provider’s display name.</li><li>Paste the provider’s full HTTPS endpoint, such as <code>https://example.com/mcp</code>. A website homepage may not be an MCP endpoint.</li><li>Use Public HTTPS for an internet service. For a service on your home or office network, select Authorized private LAN and list its exact IP addresses.</li><li>Save, then select Discover / refresh to fetch the list of tools. Review what each tool can do and what information it accepts.</li><li>Select Approve catalog &amp; enable only when you trust the tools. Saving or discovering alone does not let Jarvis call them.</li></ol>
        <p>A bearer token is a secret access key supplied by the provider. Paste it into the token field, not the URL. Leave it blank when the provider does not require one.</p>
        <p>An API key may be accepted as a Bearer token or in a separate header. Follow the provider’s instructions: choose Bearer token for Authorization: Bearer, or API key header for a header such as X-API-Key. No conversion is needed; paste the raw credential. OAuth sign-in or token exchange is not performed by this form.</p>
        <p>Certificate trust checks the service’s identity and encrypted connection. Start with System trust. Choose Private CA certificates only when your LAN service administrator supplies a PEM CA certificate; never paste a private key.</p>
      </details></header>
    {error && <p role="alert" style={{ color: 'var(--color-error)' }}>{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <form className="rounded-xl border p-5 space-y-3" style={{ borderColor: 'var(--color-border)' }} onSubmit={e => {
      e.preventDefault();
      const saved = { ...definition, ...(token || clearToken ? { credential_secret: token } : {}) };
      void perform(async () => {
        if (editing) await mcpRequest(`/${editing.id}`, 'PUT', { revision: editing.revision, definition: saved });
        else await mcpRequest('', 'POST', saved);
        reset();
      }, 'Connection saved and disabled. Discover and review the catalog before approving.');
    }}>
      <h3 className="font-medium">{editing ? `Edit ${editing.name}` : 'Add MCP connection'}</h3>
      <label className="block text-sm">Connection name
        <input className={field} style={style} required maxLength={24} pattern="[a-z][a-z0-9_]{0,23}" placeholder="home_tools"
          aria-describedby={nameHelpId} title="Use 1–24 characters: start with a lowercase letter, then use lowercase letters, digits or underscores."
          onInvalid={e => e.currentTarget.setCustomValidity('Enter a connection name with 1–24 characters. Start with a lowercase letter; use only lowercase letters, digits or underscores. Example: home_tools.')}
          value={definition.name} onChange={e => { e.currentTarget.setCustomValidity(''); setDefinition({ ...definition, name: e.target.value }); }} /></label>
      <p id={nameHelpId} className="text-xs">Use 1–24 characters. Start with a lowercase letter (a–z); use only lowercase letters, digits (0–9) or underscores (_). No spaces, capital letters or hyphens. Examples: <code>home_tools</code>, <code>weather2</code>.</p>
      <label className="block text-sm">HTTPS endpoint
        <input className={field} style={style} required type="url" maxLength={4096} placeholder="https://example.com/mcp"
          value={definition.url} onChange={e => setDefinition({ ...definition, url: e.target.value })} /></label>
      <p className="text-xs">Use HTTPS without URL credentials or queries. Local package installation remains a separate roadmap item.</p>
      <label className="block text-sm">Network access<select className={field} style={style} value={definition.network_access || 'public'} onChange={e => setDefinition({ ...definition, network_access: e.target.value as 'public' | 'lan', lan_addresses: '', tls_trust: 'system', ca_certificate: '' })}><option value="public">Public HTTPS (default)</option><option value="lan">Authorized private LAN</option></select></label>
      {definition.network_access === 'lan' && <>
        <label className="block text-sm">Authorized LAN IP addresses<textarea className={field} style={style} required maxLength={2048} placeholder="192.168.1.20, fd00::20" value={definition.lan_addresses || ''} onChange={e => setDefinition({ ...definition, lan_addresses: e.target.value })} /></label>
        <p className="text-xs">Authorize 1–32 exact RFC1918 or IPv6 unique-local addresses, not subnets. Every DNS answer must be listed. Loopback, link-local and metadata endpoints remain blocked. Saving authorizes catalog discovery at this endpoint; tool calls still require review and approval.</p>
        <label className="block text-sm">Certificate trust<select className={field} style={style} value={definition.tls_trust || 'system'} onChange={e => setDefinition({ ...definition, tls_trust: e.target.value as 'system' | 'custom_ca', ca_certificate: '' })}><option value="system">System trust</option><option value="custom_ca">Private CA certificates</option></select></label>
        {definition.tls_trust === 'custom_ca' && <label className="block text-sm">PEM CA certificates<textarea className={field} style={style} required maxLength={16384} value={definition.ca_certificate || ''} onChange={e => setDefinition({ ...definition, ca_certificate: e.target.value })} /></label>}
        <p className="text-xs">Certificate and hostname verification stay enabled. Private CA trust applies only to this connection. LAN HTTPS may use a custom port.</p>
      </>}
      <label className="block text-sm">Authentication method
        <select className={field} style={style} aria-describedby={authHelpId} value={definition.auth_type || 'bearer'} onChange={e => {
          setDefinition({ ...definition, auth_type: e.target.value as 'bearer' | 'api_key', api_key_header: e.target.value === 'api_key' ? 'X-API-Key' : '' });
          setToken(''); setClearToken(false);
        }}><option value="bearer">Bearer token (Authorization: Bearer)</option><option value="api_key">API key header</option></select>
      </label>
      <p id={authHelpId} className="text-xs">Use the method specified by your MCP provider. Bearer with no saved token allows an unauthenticated connection. An API key header requires a saved key before discovery.</p>
      {definition.auth_type === 'api_key' && <>
        <label className="block text-sm">API key header name
          <input className={field} style={style} required maxLength={64} pattern="[A-Za-z][A-Za-z0-9-]{0,63}" aria-describedby={headerHelpId} placeholder="X-API-Key" value={definition.api_key_header || ''} onChange={e => setDefinition({ ...definition, api_key_header: e.target.value })} />
        </label>
        <p id={headerHelpId} className="text-xs">Enter the provider’s header name, for example X-API-Key. Use 1–64 ASCII letters, digits or hyphens, starting with a letter. Header names are case-insensitive. Authorization, HTTP routing/transport and MCP protocol headers are reserved.</p>
      </>}
      <label className="block text-sm">{editing ? 'Replace saved credential (blank keeps it if settings are unchanged)' : definition.auth_type === 'api_key' ? 'API key' : 'Bearer token (optional)'}
        <input className={field} style={style} type="password" autoComplete="off" maxLength={8192}
          aria-describedby={credentialHelpId}
          value={token} onChange={e => { setToken(e.target.value); setClearToken(false); }} /></label>
      {editing?.has_token && <label className="flex gap-2 text-sm"><input type="checkbox" checked={clearToken}
        onChange={e => { setClearToken(e.target.checked); setToken(''); }} />Remove saved token</label>}
      <p id={credentialHelpId} className="text-xs">Paste only the raw token or key, without the Bearer prefix or header name. Use 1–8192 printable ASCII characters without spaces. Credentials are encrypted on the server and never returned to this form. Changing the endpoint, authentication method, API key header, authorized addresses or certificate trust clears a retained token unless you explicitly replace it.</p>
      <label className="flex gap-2 text-sm"><input type="checkbox" checked={definition.allow_without_confirmation}
        onChange={e => setDefinition({ ...definition, allow_without_confirmation: e.target.checked })} />
        Allow approved tool calls without per-call confirmation</label>
      <p className="text-xs">Enable this only after reviewing tools for automated use. Browser chat and scheduled tasks need this option because they cannot ask for interactive confirmation. Leave it off for clients that can ask before each call. Editing any setting withdraws approval.</p>
      <div className="flex gap-2"><button className={button} disabled={busy} type="submit">Save connection</button>
        {editing && <button className={button} disabled={busy} type="button" onClick={reset}>Cancel edit</button>}</div>
    </form>
    <section className="rounded-xl border p-5 space-y-3" style={{ borderColor: 'var(--color-border)' }}>
      <h3 className="font-medium">Import legacy MCP connections</h3>
      <p className="text-sm">Review the server's existing configuration, then select individual public HTTPS connections.
        Credentials are never shown. Import saves a disabled connection requiring confirmation, discovery and fresh approval.
        Local commands, private endpoints and tool filters need separate configuration.</p>
      <p className="text-xs">Import does not edit or disable the legacy configuration. Remove or disable migrated legacy entries
        and restart the service before approving their replacements to avoid two active paths.</p>
      <button className={button} disabled={busy} onClick={() => void perform(async () => {
        setLegacy(await reviewLegacyMCP());
      }, 'Legacy configuration reviewed. Nothing was imported or connected.')}>
        {legacy === null ? 'Review legacy configuration' : 'Refresh legacy review'}
      </button>
      {legacy !== null && <LegacyMCPReview entries={legacy} busy={busy} onImport={entry => void perform(async () => {
        await importLegacyMCP(entry);
        setLegacy(await reviewLegacyMCP());
      }, 'Connection imported disabled. Discover and review its catalog before approval.')} />}
    </section>
    {!connections.length && <p className="text-sm">No MCP connections saved.</p>}
    {connections.map(connection => <article key={connection.id} className="rounded-xl border p-5 space-y-3" style={{ borderColor: 'var(--color-border)' }}>
      <div className="flex flex-wrap justify-between gap-2"><h3 className="font-medium">{connection.name}</h3>
        <span className="text-sm">{connection.validation_error ? 'Needs repair' : connection.approved
          ? connection.enabled ? 'Approved · enabled' : 'Approved · disabled' : connection.discovered ? 'Catalog awaiting approval' : 'Needs discovery'}</span></div>
      <p className="text-sm break-all">{connection.url}</p>
      {connection.network_access === 'lan' && <p className="text-xs break-all">Authorized LAN addresses: {connection.lan_addresses}. Certificate trust: {connection.tls_trust === 'custom_ca' ? 'Private CA' : 'System'}.</p>}
      <p className="text-xs">Revision {connection.revision} · {connection.has_token ? 'Encrypted token saved' : 'No bearer token'}</p>
      {connection.validation_error && <p role="alert">{connection.validation_error}</p>}
      <MCPCatalogReview connection={connection} />
      <div className="flex flex-wrap gap-2">
        <button className={button} disabled={busy} onClick={() => void perform(
          () => mcpRequest(`/${connection.id}/discover`, 'POST', { revision: connection.revision }),
          'Catalog refreshed. Changed catalogs withdraw approval.',
        )}>Discover / refresh</button>
        {!connection.approved && <button className={button} disabled={busy || !connection.discovered || !connection.tools.length || !!connection.validation_error} onClick={() => {
          if (!window.confirm(`Approve all ${connection.tools.length} tools from ${connection.name}? Review their contracts and confirmation setting first.`)) return;
          void perform(() => mcpRequest(`/${connection.id}/approve`, 'POST', { revision: connection.revision }), 'Catalog approved and enabled.');
        }}>Approve catalog &amp; enable</button>}
        {connection.approved && <button className={button} disabled={busy} onClick={() => void perform(
          () => mcpRequest(`/${connection.id}/enabled`, 'PUT', { revision: connection.revision, enabled: !connection.enabled }),
          connection.enabled ? 'Connection disabled.' : 'Connection enabled.',
        )}>{connection.enabled ? 'Disable' : 'Enable'}</button>}
        <button className={button} disabled={busy} onClick={() => {
          setEditing(connection); setDefinition(connectionDefinition(connection)); setToken(''); setClearToken(false);
        }}>Edit</button>
        <button className={button} disabled={busy} onClick={() => void perform(async () => {
          setAudit({ name: connection.name, events: (await mcpRequest<{ events: ToolAudit[] }>(`/${connection.id}/audit`)).events });
        }, '')}>Audit history</button>
        <button className={button} disabled={busy} onClick={() => {
          if (!window.confirm(`Remove ${connection.name} and its encrypted token? Audit history will remain.`)) return;
          void perform(async () => {
            await mcpRequest(`/${connection.id}`, 'DELETE', { revision: connection.revision });
            if (editing?.id === connection.id) reset();
          }, 'Connection removed.');
        }}>Remove</button>
      </div>
    </article>)}
    {audit && <section className="rounded-xl border p-5" style={{ borderColor: 'var(--color-border)' }}>
      <h3 className="font-medium">Audit history · {audit.name}</h3>
      <ul className="text-sm space-y-2">{audit.events.map(event => <li key={event.seq}>
        {new Date(event.timestamp * 1000).toLocaleString()} · {event.event} · Revision {event.revision} · {event.actor}
      </li>)}</ul></section>}
  </section>;
}
