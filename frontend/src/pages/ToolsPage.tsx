import { useCallback, useEffect, useState } from 'react';
import { getStoredUser } from '../lib/auth';
import { editableDefinition, runtimeRequest, type RuntimeAdapter, type RuntimeDefinition, type RuntimeTool, type ToolAudit } from '../lib/runtime-tools-api';
import { RuntimeConfigurationFields, RuntimeConfigurationSummary } from '../components/RuntimeConfigurationFields';

const empty: RuntimeDefinition = { name: 'custom_', description: '', adapter_id: 'text_transform', config: { transform: 'upper' } };
const fieldClass = 'w-full rounded-lg px-3 py-2 text-sm border';
const fieldStyle = { background: 'var(--color-bg-secondary)', color: 'var(--color-text)', borderColor: 'var(--color-border)' };
const buttonClass = 'rounded-lg border px-3 py-2 text-sm cursor-pointer disabled:opacity-50';

export function ToolsPage() {
  const [tools, setTools] = useState<RuntimeTool[]>([]);
  const [adapters, setAdapters] = useState<RuntimeAdapter[]>([]);
  const [definition, setDefinition] = useState<RuntimeDefinition>({ ...empty });
  const [editing, setEditing] = useState<RuntimeTool | null>(null);
  const [events, setEvents] = useState<ToolAudit[]>([]);
  const [auditName, setAuditName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const admin = getStoredUser()?.is_admin === true;

  const load = useCallback(async () => {
    const result = await runtimeRequest<{ tools: RuntimeTool[]; adapters: RuntimeAdapter[] }>();
    setTools(result.tools);
    setAdapters(result.adapters);
  }, []);
  useEffect(() => {
    if (admin) void load().catch(e => setError(String(e.message || e)));
  }, [admin, load]);

  const perform = async (action: () => Promise<unknown>, message: string) => {
    setBusy(true); setError(''); setNotice('');
    try {
      await action();
      setNotice(message);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      // Refresh stale cards; never approve an unseen replacement revision.
      await load().catch(() => {});
    } finally { setBusy(false); }
  };

  if (!admin) return <div className="p-8">An administrator account is required to manage runtime tools.</div>;
  const selectedAdapter = adapters.find(a => a.adapter_id === definition.adapter_id);

  return <div className="flex-1 overflow-y-auto px-6 py-10" style={{ color: 'var(--color-text)' }}>
    <div className="max-w-4xl mx-auto space-y-5">
      <header>
        <h1 className="text-lg font-semibold">Runtime Tools</h1>
        <p className="text-sm mt-2" style={{ color: 'var(--color-text-secondary)' }}>
          Configure runtime tools without restarting Jarvis. Review and approve a saved definition to make it available to all accounts.
          Editing a tool withdraws approval.
        </p>
      </header>
      {error && <p role="alert" style={{ color: 'var(--color-error)' }}>{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <form className="rounded-xl border p-5 space-y-3" style={{ borderColor: 'var(--color-border)' }} onSubmit={e => {
        e.preventDefault();
        void perform(async () => {
          if (editing) await runtimeRequest(`/${editing.id}`, 'PUT', { revision: editing.revision, definition });
          else await runtimeRequest('', 'POST', definition);
          setEditing(null); setDefinition({ ...empty });
        }, 'Definition saved. Review its card before approving.');
      }}>
        <h2 className="font-medium">{editing ? `Edit ${editing.name}` : 'Add a tool'}</h2>
        <label className="block text-sm">Tool name
          <input className={fieldClass} style={fieldStyle} required maxLength={63} pattern="custom_[a-z][a-z0-9_]{0,55}"
            value={definition.name} onChange={e => setDefinition({ ...definition, name: e.target.value })} />
        </label>
        <label className="block text-sm">Description
          <textarea className={fieldClass} style={fieldStyle} required maxLength={500} rows={2}
            value={definition.description} onChange={e => setDefinition({ ...definition, description: e.target.value })} />
        </label>
        <label className="block text-sm">Tool type
          <select className={fieldClass} style={fieldStyle} value={definition.adapter_id}
            onChange={e => {
              const adapter = adapters.find(a => a.adapter_id === e.target.value);
              if (adapter) setDefinition({ name: definition.name, description: definition.description,
                adapter_id: adapter.adapter_id, config: structuredClone(adapter.default_config) });
            }}>
            {adapters.map(adapter => <option key={adapter.adapter_id} value={adapter.adapter_id}>{adapter.label}</option>)}
          </select>
        </label>
        {selectedAdapter && <>
          <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>{selectedAdapter.description}</p>
          <RuntimeConfigurationFields adapter={selectedAdapter} config={definition.config || {}}
            onChange={config => setDefinition({ ...definition, config })} />
        </>}
        <div className="flex gap-2">
          <button className={buttonClass} disabled={busy || !selectedAdapter} type="submit">Save definition</button>
          {editing && <button className={buttonClass} type="button" disabled={busy} onClick={() => {
            setEditing(null); setDefinition({ ...empty });
          }}>Cancel edit</button>}
        </div>
      </form>
      {tools.length === 0 && <p className="text-sm">No runtime tools installed.</p>}
      {tools.map(tool => <article key={tool.id} className="rounded-xl border p-5 space-y-3" style={{ borderColor: 'var(--color-border)' }}>
        <div className="flex flex-wrap gap-2 justify-between">
          <h2 className="font-medium">{tool.name}</h2>
          <span className="text-sm">{tool.status === 'invalid' ? 'Unavailable · needs repair' : tool.approved ? (tool.enabled ? 'Approved · enabled' : 'Approved · disabled') : 'Awaiting approval'}</span>
        </div>
        <p className="text-sm">{tool.description}</p>
        {tool.validation_error && <p className="text-sm" style={{ color: 'var(--color-error)' }}>{tool.validation_error}</p>}
        <p className="text-sm">Type: {adapters.find(a => a.adapter_id === tool.adapter_id)?.label || tool.adapter_id} · Revision {tool.revision}</p>
        <RuntimeConfigurationSummary adapter={adapters.find(a => a.adapter_id === tool.adapter_id)} config={tool.config} />
        <p className="text-xs break-all" style={{ color: 'var(--color-text-secondary)' }}>Definition fingerprint: {tool.fingerprint}</p>
        <div className="flex flex-wrap gap-2">
          {!tool.approved && <button className={buttonClass} disabled={busy || tool.status === 'invalid'} onClick={() => void perform(
            () => runtimeRequest(`/${tool.id}/approve`, 'POST', { revision: tool.revision }), 'Tool approved and enabled.',
          )}>Approve &amp; enable</button>}
          {tool.approved && <button className={buttonClass} disabled={busy} onClick={() => void perform(
            () => runtimeRequest(`/${tool.id}/enabled`, 'PUT', { revision: tool.revision, enabled: !tool.enabled }),
            tool.enabled ? 'Tool disabled.' : 'Tool enabled.',
          )}>{tool.enabled ? 'Disable' : 'Enable'}</button>}
          <button className={buttonClass} disabled={busy} onClick={() => {
            setEditing(tool); setDefinition(editableDefinition(tool, adapters));
          }}>Edit</button>
          <button className={buttonClass} disabled={busy} onClick={() => void perform(
            async () => {
              const result = await runtimeRequest<{ events: ToolAudit[] }>(`/${tool.id}/audit`);
              setEvents(result.events); setAuditName(tool.name);
            }, '',
          )}>Audit history</button>
          <button className={buttonClass} disabled={busy} onClick={() => {
            if (!window.confirm(`Remove ${tool.name}? Its audit history will be retained.`)) return;
            void perform(async () => {
              await runtimeRequest(`/${tool.id}`, 'DELETE', { revision: tool.revision });
              if (editing?.id === tool.id) { setEditing(null); setDefinition({ ...empty }); }
            }, 'Tool removed.');
          }}>Remove</button>
        </div>
        <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>Available automatically in tool-enabled chat after approval. For a managed agent, add {tool.name} to its tool list.</p>
      </article>)}
      {auditName && <section className="rounded-xl border p-5" style={{ borderColor: 'var(--color-border)' }}>
        <h2 className="font-medium mb-2">Audit history · {auditName}</h2>
        <ul className="space-y-2 text-sm">{events.map(event => <li key={event.seq}>
          {new Date(event.timestamp * 1000).toLocaleString()} · {event.event} · Revision {event.revision} · {event.actor}
        </li>)}</ul>
      </section>}
    </div>
  </div>;
}
