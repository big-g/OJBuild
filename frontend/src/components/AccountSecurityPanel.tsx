import { useState, type FormEvent } from 'react';
import { accountRequest, clearAuth, getStoredUser } from '../lib/auth';
export function AccountSecurityPanel() {
  const [current, setCurrent] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [code, setCode] = useState('');
  const [expires, setExpires] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const user = getStoredUser();
  const inputClass = 'w-full rounded-md border bg-background px-3 py-2 text-sm';
  async function submit(change: boolean) {
    if (busy) return;
    setError('');
    if (change && password !== confirmation) { setError('Passwords do not match.'); return; }
    setBusy(true);
    try {
      const result = await accountRequest(change ? 'password' : 'recovery-code', { current_password: current, ...(change ? { new_password: password } : {}) });
      setCurrent('');
      if (change) { clearAuth(); window.location.reload(); }
      else { setCode(result.recovery_code!); setExpires(new Date(result.expires_at! * 1000).toLocaleString()); }
    } catch (err) { setError(err instanceof Error ? err.message : 'Account update failed.'); }
    finally { setBusy(false); }
  }
  return <section className="mb-6 rounded-lg border p-4 space-y-3" aria-label="Account security">
    <h2 className="font-semibold">Account</h2>
    <p className="text-sm">Username: <strong>{user?.username || 'Sign in required'}</strong></p>
    <form className="space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); void submit(true); }}>
      <label className="block text-sm">Current password<input className={inputClass} type="password" autoComplete="current-password" required maxLength={1024} value={current} onChange={e => setCurrent(e.target.value)} /></label>
      <label className="block text-sm">New password<input className={inputClass} type="password" autoComplete="new-password" required minLength={8} maxLength={1024} value={password} onChange={e => setPassword(e.target.value)} /></label>
      <label className="block text-sm">Confirm new password<input className={inputClass} type="password" autoComplete="new-password" required minLength={8} maxLength={1024} value={confirmation} onChange={e => setConfirmation(e.target.value)} /></label>
      <p className="text-sm text-muted-foreground">Changing your password signs out all devices and cancels saved recovery codes. Sign in again afterward.</p>
      <button disabled={busy || !current} className="rounded-md border px-3 py-2 disabled:opacity-50">Change password</button>
    </form>
    <h3 className="font-medium">Account recovery</h3>
    <p className="text-sm text-muted-foreground">Enter your current password above to create a recovery code. Save it privately to retrieve your username or reset a forgotten password. A new code replaces the previous one.</p>
    <button type="button" disabled={busy || !current} onClick={() => void submit(false)} className="rounded-md border px-3 py-2 disabled:opacity-50">Generate recovery code</button>
    {code && <div role="status" className="space-y-2"><label className="block text-sm">Save this recovery code now<textarea className={inputClass} readOnly value={code} onFocus={e => e.target.select()} /></label><p className="text-sm">Expires: {expires}. Valid for one password reset. It will not be shown again after leaving this page.</p><button type="button" onClick={() => setCode('')} className="text-sm underline">I saved it — hide code</button></div>}
    {error && <p role="alert" className="text-destructive">{error}</p>}
  </section>;
}
