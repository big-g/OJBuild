import { useCallback, useEffect, useRef, useState } from 'react';
import {
  createSourceInstance, listSourceAdapters, listSourceInstances, listSourceCredentials,
  removeSourceInstance, syncSourceInstance, testSourceConfiguration, updateSourceInstance,
} from '../../lib/sources-api';
import type { SourceAdapter, SourceConfig, SourceInstance, SourceCredential } from '../../lib/sources-api';
import './SourceManagerPanel.css';
import { CredentialManagerPanel } from './CredentialManagerPanel';
import { SourceSyncControls } from './SourceSyncControls';

export function SourceConfigurationFields({
  adapter, config, onChange, credentials = [],
}: {
  credentials?: SourceCredential[];
  adapter: SourceAdapter;
  config: SourceConfig;
  onChange: (config: SourceConfig) => void;
}) {
  const fieldValue = (name: string) => config[name] ?? adapter.fields.find((field) => field.name === name)?.default_value ?? '';
  const changeField = (field: SourceAdapter['fields'][number], value: string | number | boolean) => {
    onChange({ ...config, ...field.value_updates?.[String(value)], [field.name]: value });
  };
  const visible = (field: SourceAdapter['fields'][number]) => {
    const conditions = field.visible_when ? (Array.isArray(field.visible_when) ? field.visible_when : [field.visible_when]) : [];
    return conditions.every((condition) => condition.one_of ? condition.one_of.includes(fieldValue(condition.field)) : fieldValue(condition.field) === condition.equals);
  };
  return <>
    {adapter.fields.filter(visible).map((field) => <label key={field.name} className="flex flex-col gap-1">
      {field.label}
      {field.type === 'credential' ? <select aria-label={field.label} value={String(fieldValue(field.name))} onChange={(event) => changeField(field, event.target.value)}>
        <option value="">No authentication</option>
        {credentials.filter((credential) => field.credential_kinds?.includes(credential.kind)).map((credential) => <option key={credential.id} value={credential.id}>{credential.name} · {credential.origin}</option>)}
      </select> : field.type === 'select' ? <select
        aria-label={field.label}
        required={field.required}
        value={String(fieldValue(field.name))}
        onChange={(event) => changeField(field, event.target.value)}
      >
        <option value="">Choose…</option>
        {(field.options ?? []).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select> : <input
        aria-label={field.label}
        type={field.type === 'checkbox' ? 'checkbox' : field.type === 'number' ? 'number' : 'text'}
        required={field.required}
        placeholder={field.placeholder}
        min={field.min}
        max={field.max}
        {...(field.type === 'checkbox' ? { checked: Boolean(fieldValue(field.name)) } : { value: String(fieldValue(field.name)) })}
        onChange={(event) => changeField(field, field.type === 'checkbox' ? event.target.checked
          : field.type === 'number' ? Number(event.target.value) : event.target.value)}
      />}
      {field.description && <span style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{field.description}</span>}
    </label>)}
  </>;
}

export function SourceManagerPanel() {
  const [credentials, setCredentials] = useState<SourceCredential[]>([]);
  const [adapters, setAdapters] = useState<SourceAdapter[]>([]);
  const [sources, setSources] = useState<SourceInstance[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [editing, setEditing] = useState<SourceInstance | null | undefined>(undefined);
  const [adapterId, setAdapterId] = useState('');
  const [name, setName] = useState('');
  const [config, setConfig] = useState<SourceConfig>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const generation = useRef(0);
  const adapter = adapters.find((item) => item.adapter_id === adapterId);

  const refresh = useCallback(async () => {
    const requestGeneration = ++generation.current;
    const [definitions, connections, protectedValues] = await Promise.all([listSourceAdapters(), listSourceInstances(), listSourceCredentials()]);
    if (requestGeneration !== generation.current) return;
    setCredentials(protectedValues.credentials);
    setAdapters(definitions.adapters);
    setSources(connections.sources);
    setLoaded(true);
  }, []);

  useEffect(() => {
    let active = true;
    const load = () => refresh().catch((err) => {
      if (active) setError(err instanceof Error ? err.message : String(err));
    });
    void load();
    const interval = setInterval(load, 3000);
    return () => { active = false; generation.current++; clearInterval(interval); };
  }, [refresh]);

  const perform = async (operation: () => Promise<unknown>, message: string) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await operation();
      setNotice(message);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const openEditor = (source: SourceInstance | null) => {
    setEditing(source);
    setAdapterId(source?.adapter_id ?? adapters[0]?.adapter_id ?? '');
    setName(source?.name ?? '');
    setConfig(source?.config ?? {});
    setError('');
    setNotice('');
  };

  const save = async () => {
    if (!adapter) return;
    await perform(async () => {
      if (editing) await updateSourceInstance({ ...editing, name, config });
      else await createSourceInstance(adapter.adapter_id, name, config);
      setEditing(undefined);
    }, 'Source saved. Choose Sync to index its documents.');
  };

  return <section className="source-manager hud-panel p-4 flex flex-col gap-3" aria-label="Configured sources">
    <div className="flex items-center justify-between gap-3">
      <h3 className="hud-label">Configured sources</h3>
      <button type="button" disabled={busy || !loaded || !adapters.length} onClick={() => openEditor(null)}>Add source</button>
    </div>
    <p style={{ color: 'var(--color-text-secondary)', fontSize: 12 }}>
      Manage named connections, each with its own settings and indexed documents.
    </p>
    {error && <p role="alert" style={{ color: 'var(--color-error)' }}>{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {!loaded && <p>Loading configured sources…</p>}
    {loaded && sources.length === 0 && <p>No configured sources yet.</p>}
    {sources.map((source) => <article key={source.id} className="hud-panel p-3 flex flex-col gap-2">
      <div><strong>{source.name}</strong> · {adapters.find((item) => item.adapter_id === source.adapter_id)?.display_name ?? source.adapter_id}</div>
      <div style={{ color: 'var(--color-text-secondary)', fontSize: 12 }}>
        {source.enabled ? 'Enabled' : 'Disabled'} · {(source.state === 'syncing' || source.state === 'queued' || source.latest_job?.state === 'running') ? (source.state === 'queued' ? 'Queued…' : 'Syncing…') : `${source.chunks ?? 0} indexed chunks`}
        {source.checkpoint?.last_sync && ` · Last sync ${new Date(source.checkpoint.last_sync).toLocaleString()}`}
      </div>
      <SourceSyncControls source={source} refresh={refresh} />
      {source.error && <p role="alert">{source.error}</p>}
      <div className="flex flex-wrap gap-3">
        <button disabled={busy || (source.state === 'syncing' || source.state === 'queued' || source.latest_job?.state === 'running') || !source.enabled} onClick={() => void perform(() => syncSourceInstance(source.id), 'Sync started.')}>Sync</button>
        <button disabled={busy || (source.state === 'syncing' || source.state === 'queued' || source.latest_job?.state === 'running')} onClick={() => openEditor(source)}>Edit</button>
        <button disabled={busy || (source.state === 'syncing' || source.state === 'queued' || source.latest_job?.state === 'running')} onClick={() => void perform(() => updateSourceInstance({ ...source, enabled: !source.enabled }), 'Source updated.')}>{source.enabled ? 'Disable' : 'Enable'}</button>
        <button disabled={busy || (source.state === 'syncing' || source.state === 'queued' || source.latest_job?.state === 'running')} onClick={() => {
          if (window.confirm(`Remove “${source.name}” and its indexed documents? Original files will be kept.`)) {
            void perform(() => removeSourceInstance(source), 'Source and its indexed documents removed.');
          }
        }}>Remove</button>
      </div>
    </article>)}
    <CredentialManagerPanel credentials={credentials} refresh={refresh} />
    {editing !== undefined && adapter && <form className="flex flex-col gap-3" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <fieldset disabled={busy} className="flex flex-col gap-3">
        <legend>{editing ? 'Edit source' : 'Add source'}</legend>
        <label>Source type <select value={adapterId} disabled={!!editing} onChange={(event) => {
          setAdapterId(event.target.value); setConfig({}); setNotice('');
        }}>{adapters.map((item) => <option key={item.adapter_id} value={item.adapter_id}>{item.display_name}</option>)}</select></label>
        <p>{adapter.description}</p>
        <label>Name <input aria-label="Source name" value={name} maxLength={120} required onChange={(event) => setName(event.target.value)} /></label>
        <SourceConfigurationFields credentials={credentials} adapter={adapter} config={config} onChange={(value) => { setConfig(value); setNotice(''); }} />
        {editing && <p>Changing the configuration clears this connection’s indexed documents. Sync again after saving.</p>}
        <div className="flex gap-3">
          <button type="button" onClick={() => void perform(async () => {
            const result = await testSourceConfiguration(adapterId, config);
            setConfig(result.config);
          }, 'Connection test passed. Configuration has not been saved.')}>Test connection</button>
          <button type="submit">Save source</button>
          <button type="button" onClick={() => setEditing(undefined)}>Cancel</button>
        </div>
      </fieldset>
    </form>}
  </section>;
}
