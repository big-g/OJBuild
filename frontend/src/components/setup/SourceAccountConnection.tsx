import { useEffect, useState } from 'react';
import { getBase } from '../../lib/api';
import { startOAuthFlow } from '../../lib/connectors-api';
import { disconnectSourceAccount, getSourceConnection, setSourceAccountClient, setSourceAccountToken } from '../../lib/sources-api';
import type { SourceInstance } from '../../lib/sources-api';

export function SourceAccountConnection({ source, authType, refresh }: {
  source: SourceInstance; authType: 'token' | 'oauth'; refresh: () => Promise<void>;
}) {
  const [connected, setConnected] = useState<boolean | undefined>();
  const [token, setToken] = useState('');
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [configureClient, setConfigureClient] = useState(false);
  useEffect(() => {
    let active = true;
    void getSourceConnection(source.id).then((value) => { if (active) setConnected(value.connected); })
      .catch((err) => { if (active) setError(err instanceof Error ? err.message : String(err)); });
    return () => { active = false; };
  }, [source.id, source.revision]);
  const perform = async (operation: () => Promise<unknown>) => {
    setBusy(true); setError('');
    try {
      await operation();
      setConnected((await getSourceConnection(source.id)).connected);
      await refresh();
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setToken(''); setClientSecret(''); setBusy(false); }
  };
  return <div aria-label={`Authorization for ${source.name}`} className="flex flex-col gap-2">
    <p>{connected === undefined ? 'Checking authorization…' : connected ? (authType === 'oauth' ? 'Account authorized' : 'Credential stored; sync verifies access') : 'Authorization required'}</p>
    <p>Replacing authorization or disconnecting clears this connection’s indexed documents. Sync again after authorization.</p>
    <fieldset disabled={busy} className="flex flex-col gap-2">
      {authType === 'oauth' ? <>
        <button type="button" disabled={!source.enabled} onClick={() => void perform(() => startOAuthFlow(`/v1/sources/${encodeURIComponent(source.id)}/oauth`))}>{connected ? 'Reauthorize account' : 'Authorize account'}</button>
        <button type="button" onClick={() => { setConfigureClient(!configureClient); setClientSecret(''); }}>Configure OAuth application</button>
        {configureClient && <>
          <p>Use this connection’s application registration, or authorize with the existing server registration. Register this callback URL with your provider:</p>
          <code>{`${getBase() || window.location.origin}/v1/sources/${source.id}/oauth/callback`}</code>
          <label>Client ID <input aria-label="Account client ID" autoComplete="off" value={clientId} onChange={(event) => setClientId(event.target.value)} /></label>
          <label>Client secret <input aria-label="Account client secret" type="password" autoComplete="new-password" value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} /></label>
          <button type="button" disabled={!clientId || !clientSecret} onClick={() => void perform(() => setSourceAccountClient(source, clientId, clientSecret))}>Save application and reset authorization</button>
        </>}
      </> : <>
        <label>Account token <input aria-label="Account token" type="password" autoComplete="new-password" value={token} onChange={(event) => setToken(event.target.value)} /></label>
        <button type="button" disabled={!token} onClick={() => void perform(() => setSourceAccountToken(source, token))}>{connected ? 'Replace token' : 'Store token'}</button>
        <p>Stored encrypted on the server. Sync checks provider access.</p>
      </>}
      <button type="button" onClick={() => void perform(() => disconnectSourceAccount(source))}>Disconnect this account</button>
    </fieldset>
    {error && <p role="alert">{error}</p>}
  </div>;
}
