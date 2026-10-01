import { useState } from 'react';
import { createSourceCredential, removeSourceCredential, rotateSourceCredential } from '../../lib/sources-api';
import type { SourceCredential } from '../../lib/sources-api';

export function CredentialManagerPanel({ credentials, refresh }: { credentials: SourceCredential[]; refresh: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [rotation, setRotation] = useState<SourceCredential | null>(null);
  const [name, setName] = useState('');
  const [kind, setKind] = useState<'bearer' | 'api_key'>('bearer');
  const [origin, setOrigin] = useState('');
  const [header, setHeader] = useState('X-API-Key');
  const [secret, setSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const close = () => { setSecret(''); setOpen(false); setRotation(null); };
  const perform = async (operation: () => Promise<unknown>) => {
    setBusy(true); setError(''); setNotice('');
    try { await operation(); close(); await refresh(); setNotice('Credential updated.'); }
    catch (err) { setError(err instanceof Error ? err.message : 'Credential operation failed'); }
    finally { setSecret(''); setBusy(false); }
  };
  return <div className="flex flex-col gap-3" aria-label="Protected credentials">
    <h4>Protected credentials</h4>
    <p>Reusable bearer tokens and API keys, encrypted on the server and restricted to one HTTPS origin.</p>
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {credentials.map((credential) => <div key={credential.id} className="flex flex-wrap gap-3">
      <span>{credential.name} · {credential.kind} · {credential.origin}</span>
      <button type="button" disabled={busy} onClick={() => { setSecret(''); setRotation(credential); setOpen(true); setError(''); }}>Rotate</button>
      <button type="button" disabled={busy} onClick={() => {
        if (window.confirm(`Remove credential “${credential.name}”? Detach it from all sources first.`)) void perform(() => removeSourceCredential(credential));
      }}>Remove credential</button>
    </div>)}
    <button type="button" disabled={busy} onClick={() => { setSecret(''); setRotation(null); setName(''); setOrigin(''); setOpen(true); setError(''); }}>Add credential</button>
    {open && <form onSubmit={(event) => {
      event.preventDefault();
      void perform(() => rotation ? rotateSourceCredential(rotation, secret)
        : createSourceCredential({ name, kind, origin, header_name: kind === 'api_key' ? header : '', secret }));
    }}>
      <fieldset disabled={busy} className="flex flex-col gap-3">
        <legend>{rotation ? `Rotate ${rotation.name}` : 'Add protected credential'}</legend>
        {!rotation && <>
          <label>Credential name <input required maxLength={120} value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label>Authentication <select value={kind} onChange={(e) => { setSecret(''); setKind(e.target.value as 'bearer' | 'api_key'); }}><option value="bearer">Bearer token</option><option value="api_key">API key header</option></select></label>
          <label>HTTPS origin <input required type="url" placeholder="https://api.example.com" value={origin} onChange={(e) => setOrigin(e.target.value)} /></label>
          {kind === 'api_key' && <label>Header name <input required value={header} onChange={(e) => setHeader(e.target.value)} /></label>}
        </>}
        <label>{rotation ? 'Replacement secret' : 'Secret'} <input type="password" autoComplete="new-password" required maxLength={8192} value={secret} onChange={(e) => setSecret(e.target.value)} /></label>
        <p>Secret values cannot be displayed again. Keep the server credential database and its original encryption key together in protected backups.</p>
        <div className="flex gap-3"><button type="submit">{rotation ? 'Rotate credential' : 'Save credential'}</button><button type="button" onClick={close}>Cancel</button></div>
      </fieldset>
    </form>}
  </div>;
}
