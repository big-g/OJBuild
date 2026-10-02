import { useEffect, useState } from 'react';
import { applyLegacySourceImport, listLegacySourceImports, previewLegacySourceImport } from '../../lib/sources-api';
import type { LegacySourceImport, LegacySourceImportPlan } from '../../lib/sources-api';

export function LegacySourceImportReview({ plan }: { plan: LegacySourceImportPlan }) {
  return <div aria-label="Import preview">
    <p>Import as <strong>{plan.name}</strong> using {plan.credential_storage === 'bundle' ? 'an encrypted account credential bundle for' : 'a protected credential restricted to'} {plan.credential_origin}.</p>
    {plan.oauth_grant_preserved && <>
      <p>The existing OAuth grant is preserved. Import does not narrow its permissions. Reauthorize the named account to request its current read permissions.</p>
      <p>{plan.refresh_available ? 'Refresh credentials will be copied. Both connections may share a provider grant; provider revocation or refresh-token rotation can affect both.' : 'No refresh credentials are available. Authorize the named account again when its access token expires.'}</p>
    </>}
    <p>Settings: {plan.settings.length ? plan.settings.map(({ label, value }) => `${label}: ${String(value) || 'Not set'}`).join(' · ') : 'Provider defaults'}</p>
    <p>A new, independent index will be created when you choose Sync. Existing documents and the original connection will be kept.</p>
    <p>Import does not contact the provider or start syncing. This preview expires in {Math.floor(plan.expires_in_seconds / 60)} minutes.</p>
  </div>;
}

export function LegacySourceImportsPanel({ refresh }: { refresh: () => Promise<void> }) {
  const [imports, setImports] = useState<LegacySourceImport[]>([]);
  const [selected, setSelected] = useState<LegacySourceImport | null>(null);
  const [name, setName] = useState('');
  const [plan, setPlan] = useState<LegacySourceImportPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    let active = true;
    const load = () => listLegacySourceImports().then((result) => { if (active) setImports(result.imports); })
      .catch(() => { if (active) setError('Unable to load existing connections for import.'); });
    void load();
    const interval = setInterval(load, 5000);
    return () => { active = false; clearInterval(interval); };
  }, []);

  const perform = async (action: () => Promise<void>) => {
    setBusy(true); setError(''); setNotice('');
    try { await action(); }
    catch (err) { setPlan(null); setError(err instanceof Error ? err.message : 'Import failed'); }
    finally { setBusy(false); }
  };
  return <section aria-label="Import existing connections" className="flex flex-col gap-3">
    <h4>Import existing connections</h4>
    <p>Import an existing server connection as a named source without entering its token again.</p>
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {imports.map((item) => <div key={item.import_id}>
      <span>{item.display_name} · {item.state}</span>{' '}
      {item.state === 'available' && <button type="button" disabled={busy} onClick={() => {
        setSelected(item); setName(item.display_name); setPlan(null); setError(''); setNotice('');
      }}>Import {item.display_name}</button>}
      {item.state === 'removed' && <span> Previously imported source was removed. Use Add source for a new connection.</span>}
    </div>)}
    {selected && <form onSubmit={(event) => {
      event.preventDefault();
      void perform(async () => { setPlan(await previewLegacySourceImport(selected.import_id, name)); });
    }}>
      <fieldset disabled={busy} className="flex flex-col gap-3">
        <legend>Import {selected.display_name}</legend>
        <label>Source name <input required maxLength={120} value={name} onChange={(event) => { setName(event.target.value); setPlan(null); }} /></label>
        <button type="submit">Preview import</button>
        {plan && <>
          <LegacySourceImportReview plan={plan} />
          <button type="button" onClick={() => void perform(async () => {
            await applyLegacySourceImport(plan);
            setPlan(null); setSelected(null);
            setImports((await listLegacySourceImports()).imports);
            await refresh();
            setNotice('Connection imported. Choose Sync on the new named source to index its documents.');
          })}>Apply import</button>
        </>}
        <button type="button" onClick={() => { setSelected(null); setPlan(null); }}>Cancel</button>
      </fieldset>
    </form>}
  </section>;
}
