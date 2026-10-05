import { apiFetch } from './api';
export type AccountRole = 'user' | 'administrator';
export interface ManagedAccount { user_id: string; username: string; display_name: string; is_admin: boolean; disabled: boolean }
async function request(path = '', init: RequestInit = {}) {
  const response = await apiFetch(`/v1/auth/users${path}`, init);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Account request failed (${response.status})`);
  return data;
}
export async function listAccounts(): Promise<ManagedAccount[]> { return (await request()).users; }
export async function createAccount(username: string, display_name: string, password: string, role: AccountRole): Promise<ManagedAccount> {
  return request('', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, display_name, password, role }) });
}
export async function changeAccountRole(userId: string, role: AccountRole): Promise<void> {
  await request(`/${encodeURIComponent(userId)}/role`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ role }) });
}
export async function removeAccount(userId: string): Promise<void> { await request(`/${encodeURIComponent(userId)}`, { method: 'DELETE' }); }
