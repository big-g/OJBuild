import { useState, type FormEvent } from 'react';
import { accountRequest } from '../lib/auth';
const inputClass = 'w-full rounded-md border bg-background px-3 py-2 text-sm';
export function AccountRecovery({ onBack, onRecovered }: { onBack: () => void; onRecovered: (username: string) => void }) {
  const [code, setCode] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function recover(reset: boolean) {
    if (busy) return;
    setError('');
    if (reset && password !== confirmation) { setError('Passwords do not match.'); return; }
    if (reset && password.length < 8) { setError('Use at least 8 characters.'); return; }
    setBusy(true);
    try {
      const result = await accountRequest('recover', { recovery_code: code.trim(), ...(reset ? { new_password: password } : {}) });
      if (reset) onRecovered(result.username!); else setUsername(result.username!);
    } catch (err) { setError(err instanceof Error ? err.message : 'Recovery failed.'); }
    finally { setBusy(false); }
  }
  return <div className="flex min-h-screen items-center justify-center px-4"><section className="w-full max-w-sm space-y-4">
    <h1 className="text-2xl font-semibold">Recover your account</h1>
    <p className="text-sm text-muted-foreground">Enter the recovery code saved from Account settings or supplied by your server administrator.</p>
    <label className="block">Recovery code<input className={inputClass} type="password" autoComplete="off" value={code} onChange={e => { setCode(e.target.value); setUsername(''); }} maxLength={256} /></label>
    <button type="button" disabled={busy || !code.trim()} onClick={() => void recover(false)} className="rounded-md border px-3 py-2 disabled:opacity-50">Find my username</button>
    {username && <p role="status">Your username: <strong>{username}</strong></p>}
    <form className="space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); void recover(true); }}>
      <label className="block">New password<input className={inputClass} type="password" autoComplete="new-password" required minLength={8} maxLength={1024} value={password} onChange={e => setPassword(e.target.value)} /></label>
      <label className="block">Confirm new password<input className={inputClass} type="password" autoComplete="new-password" required minLength={8} maxLength={1024} value={confirmation} onChange={e => setConfirmation(e.target.value)} /></label>
      <p className="text-sm text-muted-foreground">Resetting signs out all devices and uses up this recovery code. Your conversations stay intact.</p>
      <button disabled={busy || !code.trim()} className="w-full rounded-md bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50">{busy ? 'Working…' : 'Reset password'}</button>
    </form>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    <details className="text-sm"><summary>No recovery code?</summary><p className="mt-2">Ask the server administrator to generate one with <code>uv run jarvis auth recovery-code</code> on the OpenJarvis server. If you are the administrator, run it from <code>~/.openjarvis/src</code>. Use <code>uv run jarvis auth list-users</code> to find the username.</p></details>
    <button type="button" disabled={busy} onClick={onBack} className="text-sm underline">Back to sign in</button>
  </section></div>;
}
