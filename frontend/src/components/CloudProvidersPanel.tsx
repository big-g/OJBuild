import { useEffect, useState } from 'react';
import { getStoredUser } from '../lib/auth';
import { getCloudProviders, removeCloudProvider, saveCloudProvider, type CloudProviderSettings } from '../lib/cloud-providers-api';
import { ConfigurationHelp } from './ConfigurationHelp';

export function CloudProvidersPanel() {
  const admin = !!getStoredUser()?.is_admin;
  const [settings, setSettings] = useState<CloudProviderSettings | null>(null);
  const [provider, setProvider] = useState('openai');
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => { if (admin) void getCloudProviders().then(setSettings).catch(() => setError('Cannot load cloud providers.')); }, [admin]);
  if (!admin) return null;
  async function save() {
    if (!settings || busy || !key.trim()) return;
    const value = key.trim();
    setKey(''); setBusy(true); setError(''); setNotice('');
    try {
      setSettings(await saveCloudProvider(provider, settings.revision, value));
      setNotice('Key saved on the server. New requests use it immediately; no restart is needed.');
    } catch (e) { setError(e instanceof Error ? e.message : 'Cannot save provider key.'); }
    finally { setBusy(false); }
  }
  async function remove(id: string) {
    if (!settings || busy) return;
    setBusy(true); setKey(''); setError(''); setNotice('');
    try {
      setSettings(await removeCloudProvider(id, settings.revision));
      setNotice('Key removed for new requests. Legacy keys stay overridden.');
    } catch (e) { setError(e instanceof Error ? e.message : 'Cannot remove provider key.'); }
    finally { setBusy(false); }
  }
  return <div className="space-y-3" aria-label="Server cloud provider API keys">
    <ConfigurationHelp title="Cloud provider API keys" summary="Shared server credentials; only administrators can manage them.">
      Keys are encrypted in the server credential vault and never returned to the browser. Saving or replacing a key affects new cloud requests from all consoles; requests already running can finish with the previous key. Remove overrides legacy environment/file keys, including after restart. A configured key has not been tested with the provider and may require provider billing and server SDK packages. Existing tool permissions, evidence checks and account policies still apply. Choose a cloud model explicitly to use it; adding a key does not switch the default model. This screen configures the providers supported by the installed inference adapters.
    </ConfigurationHelp>
    <div className="flex flex-wrap items-end gap-3">
      <label className="text-sm">Provider<select aria-label="Cloud provider" value={provider} disabled={!settings || busy} onChange={e => { setProvider(e.target.value); setKey(''); }} className="block rounded border p-2" style={{ background: 'var(--color-bg)' }}>
        {settings?.providers.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select></label>
      <label className="text-sm">API key<input type="password" autoComplete="new-password" aria-label="Cloud provider API key" value={key} onChange={e => setKey(e.target.value)} disabled={!settings || busy} placeholder="Paste a new key" className="block rounded border p-2" style={{ background: 'var(--color-bg)' }} /></label>
      <button disabled={!settings || busy || !key.trim()} onClick={() => void save()} className="rounded border px-3 py-2">{busy ? 'Saving…' : 'Save provider key'}</button>
      <button disabled={busy} onClick={() => { setKey(''); setError(''); void getCloudProviders().then(setSettings).catch(() => setError('Cannot load cloud providers.')); }} className="rounded border px-3 py-2">Reload providers</button>
    </div>
    <div className="space-y-2">{settings?.providers.map(p => <div key={p.id} className="flex flex-wrap items-center gap-3 text-sm">
      <span className="font-medium">{p.name}</span>
      <span>{p.configured ? (p.source === 'legacy' ? 'Configured through legacy settings' : 'Configured on server') : 'No key configured'}</span>
      {p.configured && <button disabled={busy} aria-label={`Remove ${p.name} key`} onClick={() => void remove(p.id)} className="underline">Remove key</button>}
    </div>)}</div>
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </div>;
}
