import { useEffect, useState, type FormEvent } from 'react';
import { ConfigurationHelp } from './ConfigurationHelp';
import { ContentReviewPanel } from './ContentReviewPanel';
import { getStoredUser } from '../lib/auth';
import { setAccountBirthdate, setImageGenerationAccess, changeAccountRole, createAccount, listAccounts, removeAccount, type AccountRole, type ManagedAccount } from '../lib/accounts-api';

export function AccountManagementPanel() {
  const [accounts, setAccounts] = useState<ManagedAccount[]>([]);
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [birthdate, setBirthdate] = useState('');
  const [birthdateTimezone, setBirthdateTimezone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
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
    <form className="space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); void act(async () => { await createAccount(username.trim(), displayName.trim(), password, role, birthdate || null, birthdateTimezone); setBirthdate(''); setPassword(''); setUsername(''); setDisplayName(''); setRole('user'); }, 'Account created.'); }}>
      <label className="block text-sm">Username<input className={input} autoComplete="off" required maxLength={128} value={username} onChange={e => setUsername(e.target.value)} /></label>
      <label className="block text-sm">Display name<input className={input} required maxLength={128} value={displayName} onChange={e => setDisplayName(e.target.value)} /></label>
      <label className="block text-sm">Initial password<input className={input} type="password" autoComplete="new-password" required minLength={8} maxLength={1024} value={password} onChange={e => setPassword(e.target.value)} /></label>
      <label className="block text-sm">Account role<select className={input} value={role} onChange={e => setRole(e.target.value as AccountRole)}><option value="user">User</option><option value="administrator">Administrator</option></select></label>
      <label className="block text-sm">Birthdate (optional)<input aria-label="New account birthdate" className={input} type="date" min="1900-01-01" value={birthdate} onChange={e => setBirthdate(e.target.value)} /></label>
      <label className="block text-sm">Birthday time zone<input className={input} value={birthdateTimezone} onChange={e => setBirthdateTimezone(e.target.value)} placeholder="America/New_York" /></label>
      <button disabled={busy} className="rounded-md border px-3 py-2 disabled:opacity-50">Create account</button>
    </form>
    <ul className="space-y-3">{accounts.map(account => <li key={account.user_id} className="rounded border p-3 space-y-2">
      <p>{account.display_name} ({account.username}) — <strong>{account.is_admin ? 'Administrator' : 'User'}</strong>{account.disabled ? ' — disabled' : ''}</p>
      <BirthdateEditor key={`${account.user_id}-${account.birthdate}-${account.birthdate_timezone}`} birthdate={account.birthdate ?? ''} timezone={account.birthdate_timezone || 'UTC'} busy={busy} save={(birthdate, zone) => act(() => setAccountBirthdate(account.user_id, birthdate, zone), 'Birthdate and content protection updated.')} />
      <p className="text-sm">Current age: {account.age ?? 'unknown'}</p>
      <p className="text-sm">{account.age == null || account.age < 18 ? 'Protected: image requests and results require administrator review.' : 'Adult: standard generation permissions apply.'}</p>
      <label className="flex gap-2 items-center text-sm"><input type="checkbox" checked={!!account.image_generation_enabled} disabled={busy || account.disabled} onChange={e => void act(() => setImageGenerationAccess(account.user_id, e.target.checked), 'Direct image submission access updated.')} />Allow direct image submission</label>
      <ConfigurationHelp title="Direct image submission" summary="Allows images without chat model review. Jarvis tools remain available when disabled."><p>When enabled, this account can submit image prompts directly from Generate image or run image workflows from the workflow screen. When disabled, image generation is available through Jarvis chat tools only. Existing operation approvals still apply. Changes affect new submissions; accepted jobs continue.</p><p>Age-based protection applies independently: users under 18 and users without a recorded birthdate require administrator review of requests and generated images, including when direct submission is enabled.</p></ConfigurationHelp>
      {account.user_id === user.user_id ? <p className="text-sm">Your account. Use another administrator to change your role or remove this account.</p> : <>
        <button disabled={busy} type="button" className="rounded border px-3 py-1 mr-2" onClick={() => { if (window.confirm(`Change ${account.username} to ${account.is_admin ? 'User' : 'Administrator'}? This signs them out on every device.`)) void act(() => changeAccountRole(account.user_id, account.is_admin ? 'user' : 'administrator'), 'Role updated.'); }}>{account.is_admin ? 'Make user' : 'Make administrator'}</button>
        <button disabled={busy} type="button" className="rounded border px-3 py-1" onClick={() => { if (window.confirm(`Remove account ${account.username}? Login and recovery credentials will be removed. Historical data remains tied to its original identity.`)) void act(() => removeAccount(account.user_id), 'Account removed.'); }}>Remove account</button>
      </>}
    </li>)}</ul>
    <ConfigurationHelp title="Age-based image protection" summary="Under 18 or unknown age requires request and image review.">Only administrators can record or change birthdates. Missing birthdates are protected. Protection lifts automatically at midnight on the 18th birthday in the configured time zone. February 29 birthdays advance on March 1 in non-leap years. Administrators whose birthdate establishes adulthood approve requests before generation and review the image before the user can download it. Unreviewable 3D generation is blocked for protected accounts. This feature covers local generated images, not general chat or external websites.</ConfigurationHelp>
    <ContentReviewPanel />
    {error && <p role="alert" className="text-destructive">{error}</p>}{message && <p role="status">{message}</p>}
  </section>;
}

function BirthdateEditor({ birthdate, timezone, busy, save }: { birthdate: string; timezone: string; busy: boolean; save: (birthdate: string | null, timezone: string) => Promise<void> }) {
  const [value, setValue] = useState(birthdate);
  const [zone, setZone] = useState(timezone);
  return <div className="flex flex-wrap gap-2 items-center"><label>Birthdate <input aria-label="Account birthdate" type="date" min="1900-01-01" value={value} onChange={e => setValue(e.target.value)} className="border rounded p-1" /></label><label>Birthday time zone <input aria-label="Birthday time zone" value={zone} onChange={e => setZone(e.target.value)} className="border rounded p-1" /></label><button type="button" disabled={busy || !zone} onClick={() => void save(value || null, zone)} className="border rounded px-2">Save birthdate</button></div>;
}
