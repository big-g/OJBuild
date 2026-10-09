import { useEffect, useState } from 'react';
import { getStoredUser } from '../lib/auth';
import { fetchModels } from '../lib/api';
import { useAppStore } from '../lib/store';
import { getAdministratorSettings, saveAdministratorSettings, type AdministratorSettings, type AdministratorField } from '../lib/admin-settings-api';
import type { ModelInfo } from '../types';

const DEFAULT = 'intelligence.default_model';
const MEMORY = 'tools.storage.extraction_model';
const SPECIAL = new Set([DEFAULT, MEMORY, 'server.model']);

export function parseAdministratorValue(field: AdministratorField, value: string): unknown {
  if (field.type === 'str') return value;
  if (field.type === 'list') {
    const parsed = JSON.parse(value);
    if (!Array.isArray(parsed) || !parsed.every(v => ['string', 'number', 'boolean'].includes(typeof v))) throw new Error(`${field.key}: enter a JSON array of text, numbers or booleans.`);
    return parsed;
  }
  const parsed = Number(value);
  if (!value.trim() || !Number.isFinite(parsed) || (field.type === 'int' && !Number.isInteger(parsed))) throw new Error(`${field.key}: enter a finite ${field.type === 'int' ? 'whole' : 'decimal'} number.`);
  return parsed;
}

export function AdministratorSettingsPanel() {
  const admin = !!getStoredUser()?.is_admin;
  const [config, setConfig] = useState<AdministratorSettings | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [changes, setChanges] = useState<Record<string, unknown>>({});
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  async function refresh() {
    const [settings, installed] = await Promise.all([getAdministratorSettings(), fetchModels().catch(() => { setError('Cannot refresh the model catalog. Other server parameters remain editable; reload when the backend is available.'); return [] as ModelInfo[]; })]);
    setConfig(settings); setModels(installed); setChanges({});
  }
  useEffect(() => { if (admin) void refresh().catch(e => setError(String(e.message || e))); }, [admin]);
  if (!admin) return null;
  const installed = models.filter(m => !m.id.startsWith('oj/'));
  const byKey = new Map(config?.fields.map(f => [f.key, f]));
  const value = (field: AdministratorField) => field.key in changes ? (changes[field.key] === null ? field.bootstrap_value : changes[field.key]) : field.value;
  async function save() {
    if (!config || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const parsed: Record<string, unknown> = {};
      for (const [key, raw] of Object.entries(changes)) {
        const field = byKey.get(key)!;
        parsed[key] = raw === null || (key === DEFAULT && raw === '') || typeof raw === 'boolean' ? (key === DEFAULT && raw === '' ? null : raw) : parseAdministratorValue(field, String(raw));
      }
      const next = await saveAdministratorSettings(config.revision, parsed);
      setConfig(next); setChanges({});
      setNotice('Settings saved. Model defaults apply to future requests and memory jobs. Fields marked Restart required activate when the server restarts.');
      await fetchModels().then(refreshed => { useAppStore.getState().setModels(refreshed); setModels(refreshed); }).catch(() => setError('Settings saved, but the model catalog could not be refreshed. Reload models when the backend is available.'));
    } catch (e) { setError(e instanceof Error ? e.message : 'Cannot save settings'); }
    finally { setBusy(false); }
  }
  const filtered = config?.fields.filter(f => !SPECIAL.has(f.key) && `${f.key} ${f.help || f.reason || ''}`.toLowerCase().includes(query.toLowerCase())) || [];
  const sections = [...new Set(filtered.map(f => f.key.split('.')[0]))];
  return <div id="administrator-settings" className="space-y-3 rounded-lg border p-4" style={{ borderColor: 'var(--color-border)' }}>
    <h3 className="font-medium">Administrator server settings</h3>
    <p className="text-sm">Shared across accounts and saved in the database. TOML supplies bootstrap values; Reset removes a saved override. Your explicit chat model and task assignments take precedence over the server default.</p>
    <p className="text-sm">The memory model runs after chat to extract durable facts. Using the same model avoids loading Qwen just for memory extraction.</p>
    {[DEFAULT, MEMORY].map(key => {
      const field = byKey.get(key);
      const current = field ? String(value(field) ?? '') : '';
      return <div key={key} className="space-y-1">
        <label htmlFor={`admin-${key}`} className="block text-sm font-medium">{key === DEFAULT ? 'Server default model' : 'Memory extraction model'}</label>
        <select id={`admin-${key}`} aria-describedby={`hint-${key}`} value={current} disabled={!config || busy} onChange={e => setChanges(c => ({ ...c, [key]: e.target.value }))} className="w-full rounded border p-2" style={{ background: 'var(--color-bg-secondary)' }}>
          <option value="">{key === MEMORY ? 'Follow server default' : 'Bootstrap / startup selection'}</option>
          {current && !installed.some(m => m.id === current) && <option value={current}>{current} (not in current catalog)</option>}
          {installed.map(m => <option key={m.id} value={m.id}>{m.display_name || m.id}</option>)}
        </select>
        <p id={`hint-${key}`} className="text-xs">{field?.help} Existing-backend models are selectable here; configured-server models use task assignments.</p>
        {field?.overridden && <button disabled={busy} className="text-xs underline" onClick={() => setChanges(c => ({ ...c, [key]: null }))}>Reset to bootstrap</button>}
      </div>;
    })}
    <details><summary className="cursor-pointer">All server parameters</summary>
      <p className="text-sm my-2">Model defaults apply live. Other parameters require an OpenJarvis server restart. Protected credentials and structured integrations use their dedicated screens; this editor never displays saved secrets. These are OpenJarvis parameters; Ollama daemon and operating-system settings are managed separately.</p>
      <label className="block text-sm">Find a parameter<input value={query} onChange={e => setQuery(e.target.value)} type="search" className="block w-full rounded border p-2" /></label>
      {sections.map(section => <details key={section} open={!!query} className="my-3"><summary className="cursor-pointer font-medium">{section}</summary>
        {filtered.filter(f => f.key.split('.')[0] === section).map(field => <div key={field.key} className="space-y-1 border-b py-3">
          <label htmlFor={`admin-${field.key}`} className="block text-sm font-medium">{field.key}</label>
          {!field.editable ? <p className="text-xs">{field.reason}</p> : <>
            {field.type === 'bool' ? <input id={`admin-${field.key}`} aria-describedby={`hint-${field.key}`} type="checkbox" checked={!!value(field)} disabled={busy} onChange={e => setChanges(c => ({ ...c, [field.key]: e.target.checked }))} /> : <textarea id={`admin-${field.key}`} aria-describedby={`hint-${field.key}`} rows={field.type === 'list' ? 3 : 1} disabled={busy} value={Array.isArray(value(field)) ? JSON.stringify(value(field)) : String(value(field) ?? '')} onChange={e => setChanges(c => ({ ...c, [field.key]: e.target.value }))} className="block w-full rounded border p-2" />}
            <p id={`hint-${field.key}`} className="text-xs">{field.help} Type: {field.type === 'list' ? `JSON array of ${field.item_type} values` : field.type}. {field.minimum !== undefined && <>Range: {field.minimum}–{field.maximum}. </>}{field.pending_restart ? 'Saved; restart required.' : 'Applies after server restart.'}</p>
            {field.overridden && <button disabled={busy} className="text-xs underline" onClick={() => setChanges(c => ({ ...c, [field.key]: null }))}>Reset to bootstrap</button>}
          </>}
        </div>)}
      </details>)}
    </details>
    {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    {notice && <p role="status" className="text-sm">{notice}</p>}
    <div className="flex gap-3"><button disabled={!config || busy || !Object.keys(changes).length} onClick={() => void save()} className="rounded border px-3 py-2">{busy ? 'Saving…' : 'Save server settings'}</button>
      <button disabled={busy} onClick={() => { setError(''); void refresh().catch(e => setError(String(e.message || e))); }} className="rounded border px-3 py-2">Reload settings and models</button></div>
  </div>;
}
