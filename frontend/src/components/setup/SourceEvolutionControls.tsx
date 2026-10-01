import { useEffect, useState } from 'react';
import { applySourceMigration, listSourceAudit, previewSourceMigration } from '../../lib/sources-api';
import type { SourceAuditEvent, SourceInstance, SourceMigrationPlan } from '../../lib/sources-api';

export function SourceAuditHistory({ sourceId }: { sourceId?: string }) {
  const [events, setEvents] = useState<SourceAuditEvent[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [more, setMore] = useState(false);
  const load = async (older = false) => {
    setBusy(true); setError('');
    try {
      const page = await listSourceAudit(sourceId, older && events?.length ? events[events.length - 1].id : undefined);
      setEvents(previous => older ? [...(previous ?? []), ...page.events] : page.events);
      setMore(page.events.length === 50);
    } catch (err) { setError(err instanceof Error ? err.message : 'Audit history could not load'); }
    finally { setBusy(false); }
  };
  return <div>
    <button type="button" disabled={busy} onClick={() => events ? setEvents(null) : void load()}>{events ? 'Hide configuration history' : sourceId ? 'Configuration history' : 'All configuration history (including removed sources)'}</button>
    {error && <p role="alert">{error}</p>}
    {events && <>
      <p>Committed changes only. Configuration values and credentials are excluded.</p>
      <ul>{events.length ? events.map(event => <li key={event.id}>
        {new Date(event.created_at).toLocaleString()} · {sourceId ? '' : `${event.source_id} · `}{event.action} · {event.actor} · revision {event.revision}
        {event.schedule_revision !== null && ` · schedule revision ${event.schedule_revision}`}
        {` · version ${event.config_version}`}{event.changed_fields.length > 0 && ` · ${event.changed_fields.join(', ')}`}{event.index_reset && ' · index reset'}
      </li>) : <li>No configuration changes recorded.</li>}</ul>
      <button type="button" disabled={busy} onClick={() => void load()}>Refresh history</button>
      {more && <button type="button" disabled={busy} onClick={() => void load(true)}>Load older changes</button>}
    </>}
  </div>;
}

export function SourceEvolutionControls({ source, refresh }: { source: SourceInstance; refresh: () => Promise<void> }) {
  const [plan, setPlan] = useState<SourceMigrationPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => { setPlan(null); setNotice(''); }, [source.revision, source.adapter_config_version]);
  const active = source.state === 'syncing' || source.state === 'queued' || source.latest_job?.state === 'running';
  const perform = async (operation: () => Promise<void>) => {
    setBusy(true); setError('');
    try { await operation(); }
    catch (err) { setPlan(null); setError(err instanceof Error ? err.message : 'Migration failed'); }
    finally { setBusy(false); }
  };
  return <div aria-label={`Configuration controls for ${source.name}`}>
    {source.configuration_state === 'unsupported' && <p role="alert">This configuration version is unsupported. Install an adapter with a complete migration path before syncing.</p>}
    {source.configuration_state === 'migration_available' && <>
      <p>Configuration upgrade available: version {source.config_version} to {source.adapter_config_version}. Sync is paused until upgraded.</p>
      <button type="button" disabled={busy || active} onClick={() => void perform(async () => { setPlan(await previewSourceMigration(source)); })}>Preview configuration upgrade</button>
    </>}
    {plan && <div>
      <p>Upgrade version {plan.from_version} to {plan.to_version}. {plan.index_reset ? 'Indexed documents and checkpoint will be cleared. Sync again after upgrading.' : 'Indexed documents and checkpoint will be retained.'}</p>
      <pre>{JSON.stringify(plan.config, null, 2)}</pre>
      <button type="button" disabled={busy || active} onClick={() => void perform(async () => { await applySourceMigration(plan); setPlan(null); await refresh(); setNotice('Configuration upgraded.'); })}>Apply configuration upgrade</button>
      <button type="button" disabled={busy} onClick={() => setPlan(null)}>Dismiss preview</button>
    </div>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <SourceAuditHistory sourceId={source.id} />
  </div>;
}
