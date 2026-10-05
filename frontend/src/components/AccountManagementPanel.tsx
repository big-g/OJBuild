import { useEffect, useState, type FormEvent } from 'react';
import { getStoredUser } from '../lib/auth';
import { changeAccountRole, createAccount, listAccounts, removeAccount, type AccountRole, type ManagedAccount } from '../lib/accounts-api';

export function AccountManagementPanel() {
  const [accounts, setAccounts] = useState<ManagedAccount[]>([]);
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<AccountRole>('user');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const user = getStoredUser();
  async function refresh() { setAccounts(await listAccounts()); }
  useEffect(() => { if (user?.is_admin) void refresh().catch(e => setError(e instanceof Error ? e.message : 'Cannot load accounts')); }, [user?.is_admin]);
  async function act(action: () => Promise<unknown>, success: string) {
    if (busy) return;
    setBusy(true); setError(''); setMessage('');
    try { await action(); await refresh(); setMessage(success); }
    catch (e) { setError(e instanceof Error ? e.message : 'Account update failed'); }
    finally { setBusy(false); }
  }
  if (!user?.is_admin) return null;
  const input = 'w-full rounded-md border bg-background px-3 py-2 text-sm';
  return <section className="mb-6 rounded-lg border p-4 space-y-3" aria-label="Account management">
    <h2 className="font-semibold">Account management</h2>
    <p className="text-sm">Users manage their own chats, files and data sources. Administrators also manage accounts and shared server settings. Role changes sign the affected account out on every device.</p>
    <form className="space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); void act(async () => { await createAccount(username.trim(), displayName.trim(), password, role); setPassword(''); setUsername(''); setDisplayName(''); setRole('user'); }, 'Account created.'); }}>
      <label className="block text-sm">Username<input className={input} autoComplete="off" required maxLength={128} value={username} onChange={e => setUsername(e.target.value)} /></label>
      <label className="block text-sm">Display name<input className={input} required maxLength={128} value={displayName} onChange={e => setDisplayName(e.target.value)} /></label>
      <label className="block text-sm">Initial password<input className={input} type="password" autoComplete="new-password" required minLength={8} maxLength={1024} value={password} onChange={e => setPassword(e.target.value)} /></label>
      <label className="block text-sm">Account role<select className={input} value={role} onChange={e => setRole(e.target.value as AccountRole)}><option value="user">User</option><option value="administrator">Administrator</option></select></label>
      <button disabled={busy} className="rounded-md border px-3 py-2 disabled:opacity-50">Create account</button>
    </form>
    <ul className="space-y-3">{accounts.map(account => <li key={account.user_id} className="rounded border p-3 space-y-2">
      <p>{account.display_name} ({account.username}) — <strong>{account.is_admin ? 'Administrator' : 'User'}</strong>{account.disabled ? ' — disabled' : ''}</p>
      {account.user_id === user.user_id ? <p className="text-sm">Your account. Use another administrator to change your role or remove this account.</p> : <>
        <button disabled={busy} type="button" className="rounded border px-3 py-1 mr-2" onClick={() => { if (window.confirm(`Change ${account.username} to ${account.is_admin ? 'User' : 'Administrator'}? This signs them out on every device.`)) void act(() => changeAccountRole(account.user_id, account.is_admin ? 'user' : 'administrator'), 'Role updated.'); }}>{account.is_admin ? 'Make user' : 'Make administrator'}</button>
        <button disabled={busy} type="button" className="rounded border px-3 py-1" onClick={() => { if (window.confirm(`Remove account ${account.username}? Login and recovery credentials will be removed. Historical data remains tied to its original identity.`)) void act(() => removeAccount(account.user_id), 'Account removed.'); }}>Remove account</button>
      </>}
    </li>)}</ul>
    {error && <p role="alert" className="text-destructive">{error}</p>}{message && <p role="status">{message}</p>}
  </section>;
}
